import os
from fastapi import Header, HTTPException, status


def require_api_key(x_api_key: str = Header(..., alias="X-API-Key")) -> None:
    secret = os.getenv("API_SECRET_KEY", "")
    if not secret:
        raise HTTPException(status_code=500, detail="API_SECRET_KEY not configured on server")
    if x_api_key != secret:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
