import subprocess
import json
import re
import logging
import tempfile
import os
from fastapi import HTTPException
from pydantic import BaseModel
from typing import List, Dict, Any

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Pydantic models for structured output
class Vulnerability(BaseModel):
    tool: str
    issue: str
    severity: str
    description: str
    location: Dict[str, Any]

class ScanReport(BaseModel):
    solidity_version: str
    contract_name: str
    tools_used: List[str]
    vulnerabilities: List[Vulnerability]
    summary: Dict[str, Any]

def extract_solidity_version(code: str) -> str:
    match = re.search(r"pragma solidity\s+([\^~>=]*\d+\.\d+\.\d+)", code)
    if match:
        version = match.group(1).strip()
        if "^" in version or "~" in version or ">=" in version:
            version = version.replace("^", "").replace("~", "").split(" ")[0]
        logger.info(f"Detected Solidity version: {version}")
        return version
    return None

def extract_contract_name(code: str) -> str:
    match = re.search(r"contract\s+(\w+)\s*{", code)
    return match.group(1) if match else "Unknown"

def install_solc_version(version: str):
    try:
        subprocess.run(["solc-select", "versions"], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError:
        raise HTTPException(status_code=500, detail="solc-select is not working properly.")

    installed_versions = subprocess.run(["solc-select", "versions"], capture_output=True, text=True).stdout
    if version not in installed_versions:
        logger.info(f"Installing Solidity {version}...")
        install_proc = subprocess.run(["solc-select", "install", version], capture_output=True, text=True)
        if install_proc.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Failed installing solc {version}: {install_proc.stderr}")
    logger.info(f"Switching to Solidity {version}...")
    use_proc = subprocess.run(["solc-select", "use", version], capture_output=True, text=True)
    if use_proc.returncode != 0:
        raise HTTPException(status_code=500, detail=f"Failed switching to solc {version}: {use_proc.stderr}")

def run_mythril(contract_path: str) -> List[Vulnerability]:
    abs_path = os.path.abspath(contract_path)
    cmd = [
        "myth", "analyze", abs_path,
        "-o", "json",
        "--solc-args", "--base-path . --include-path node_modules",
        "--execution-timeout", "60"
    ]
    logger.info("Running Mythril: " + " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        logger.error(f"Mythril failed: {proc.stderr}")
        raise HTTPException(status_code=500, detail=f"Mythril failed: {proc.stderr}")
    try:
        output = json.loads(proc.stdout) if proc.stdout else []
        findings = []
        for issue in output.get("issues", []):
            findings.append(Vulnerability(
                tool="mythril",
                issue=issue["title"],
                severity=issue.get("severity", "Medium"),
                description=issue["description"],
                location={"file": issue.get("filename", ""), "line": issue.get("lineno", 0)}
            ))
        return findings
    except json.JSONDecodeError:
        logger.error(f"Mythril output invalid: {proc.stdout}")
        raise HTTPException(status_code=500, detail="Mythril output is not valid JSON")

def run_slither(contract_path: str) -> List[Vulnerability]:
    abs_path = os.path.abspath(contract_path)
    cmd = [
        "slither", abs_path,
        "--json", "-",  # Output to stdout as JSON
        "--solc-remaps", "@openzeppelin=node_modules/@openzeppelin"
    ]
    logger.info("Running Slither: " + " ".join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        logger.error(f"Slither failed with return code {proc.returncode}: stdout='{proc.stdout}', stderr='{proc.stderr}'")
        return [Vulnerability(
            tool="slither",
            issue="Analysis failed",
            severity="Error",
            description=f"Slither exited with code {proc.returncode}: {proc.stdout or proc.stderr or 'No output'}",
            location={}
        )]
    try:
        if not proc.stdout:
            logger.warning("Slither produced no output")
            return []
        output = json.loads(proc.stdout)
        findings = []
        if "results" in output and "detectors" in output["results"]:
            for issue in output["results"]["detectors"]:
                findings.append(Vulnerability(
                    tool="slither",
                    issue=issue["check"],
                    severity=issue.get("impact", "Medium"),
                    description=issue["description"],
                    location={
                        "file": issue["elements"][0]["source_mapping"]["filename_relative"],
                        "line": issue["elements"][0]["source_mapping"]["lines"][0]
                    } if issue["elements"] else {}
                ))
        return findings
    except json.JSONDecodeError:
        logger.error(f"Slither output invalid JSON: {proc.stdout}")
        return [Vulnerability(
            tool="slither",
            issue="Invalid output",
            severity="Error",
            description="Slither output is not valid JSON",
            location={}
        )]

def generate_summary(findings: List[Vulnerability]) -> Dict[str, Any]:
    total = len(findings)
    severity_counts = {"High": 0, "Medium": 0, "Low": 0, "Error": 0}
    for finding in findings:
        severity = finding.severity
        if severity in severity_counts:
            severity_counts[severity] += 1
        else:
            severity_counts["Medium"] += 1  # Default to Medium if unknown
    return {"total_issues": total, "severity_breakdown": severity_counts}

def perform_scan(file_path: str = None, code: str = None) -> dict:
    if code:
        temp_dir = tempfile.mkdtemp()
        contract_path = os.path.join(temp_dir, "contract.sol")
        with open(contract_path, "w") as f:
            f.write(code)
    elif file_path:
        contract_path = file_path
    else:
        raise HTTPException(status_code=400, detail="No contract code provided.")

    try:
        with open(contract_path, "r") as f:
            solidity_code = f.read()

        solidity_version = extract_solidity_version(solidity_code)
        if not solidity_version:
            raise HTTPException(status_code=400, detail="Could not detect Solidity version.")

        contract_name = extract_contract_name(solidity_code)
        install_solc_version(solidity_version)

        tools_used = ["mythril", "slither"]
        vulnerabilities = []
        vulnerabilities.extend(run_mythril(contract_path))
        vulnerabilities.extend(run_slither(contract_path))

        report = ScanReport(
            solidity_version=solidity_version,
            contract_name=contract_name,
            tools_used=tools_used,
            vulnerabilities=vulnerabilities,
            summary=generate_summary(vulnerabilities)
        )
        return report.dict()
    finally:
        if code and os.path.exists(contract_path):
            os.remove(contract_path)
        if code and os.path.exists(temp_dir):
            os.rmdir(temp_dir)