from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.audit import router as audit_router
from api.scan import router as scan_router

app = FastAPI(title="KONSEC API", version="1.0.0")

# Configure CORS as needed
origins = [
    "http://localhost:3000",
    "https://sec3-an3.vercel.app"  # Or your Vercel domain
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register your API endpoints
app.include_router(audit_router, prefix="/api")
app.include_router(scan_router, prefix="/api")

@app.get("/")
def home():
    return {"message": "Welcome to KONSEC API"}
