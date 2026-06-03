import os
from http import HTTPStatus
from enum import Enum
from typing import Dict, List, Optional
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import cv2
import numpy as np

SERVICE_NAME = os.getenv("SERVICE_NAME", "camera-stream")
SERVICE_VERSION = os.getenv("SERVICE_VERSION", "0.4.0")
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "local-dev-token")

app = FastAPI(
    title="FIT4110 Lab 04 - Camera Stream Service",
    version=SERVICE_VERSION,
    description="Dockerized Camera Stream API using opencv-python-headless for mock frames.",
)

class ResolutionEnum(str, Enum):
    res_480p = "640x480"
    res_720p = "1280x720"
    res_1080p = "1920x1080"

class CameraStatusEnum(str, Enum):
    active = "active"
    inactive = "inactive"

class ProblemDetails(BaseModel):
    type: str = "about:blank"
    title: str
    status: int = Field(..., ge=400, le=599)
    detail: str
    instance: Optional[str] = None

class HealthResponse(BaseModel):
    status: str
    service: str
    version: str

class CameraRegister(BaseModel):
    camera_id: str = Field(..., min_length=3, pattern="^CAM-[0-9]{3}$", examples=["CAM-001"])
    location: str = Field(..., min_length=3, examples=["Hallway-A"])
    resolution: ResolutionEnum = Field(default=ResolutionEnum.res_720p, examples=["1280x720"])
    fps: int = Field(
        ...,
        ge=1,
        le=60,
        description="Boundary range for camera FPS: 1 to 60.",
        examples=[30]
    )

class CameraInfo(BaseModel):
    camera_id: str
    location: str
    resolution: ResolutionEnum
    fps: int
    status: CameraStatusEnum
    registered_at: str

CAMERAS: Dict[str, Dict] = {}

def build_problem(
    *,
    status_code: int,
    title: str,
    detail: str,
    instance: Optional[str] = None,
    problem_type: str = "about:blank",
) -> Dict:
    problem = {
        "type": problem_type,
        "title": title,
        "status": status_code,
        "detail": detail,
    }
    if instance:
        problem["instance"] = instance
    return problem

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict):
        problem = exc.detail
    else:
        problem = build_problem(
            status_code=exc.status_code,
            title=HTTPStatus(exc.status_code).phrase if exc.status_code in HTTPStatus._value2member_map_ else "HTTP Error",
            detail=str(exc.detail),
            instance=str(request.url.path),
        )

    problem.setdefault("status", exc.status_code)
    problem.setdefault("title", HTTPStatus(exc.status_code).phrase if exc.status_code in HTTPStatus._value2member_map_ else "HTTP Error")
    problem.setdefault("type", "about:blank")
    problem.setdefault("detail", "Request failed")
    problem.setdefault("instance", str(request.url.path))

    return JSONResponse(
        status_code=exc.status_code,
        content=problem,
        media_type="application/problem+json",
        headers=getattr(exc, "headers", None),
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    first_error = exc.errors()[0] if exc.errors() else {}
    location = ".".join(str(item) for item in first_error.get("loc", []))
    message = first_error.get("msg", "Request validation error")
    detail = f"{location}: {message}" if location else message

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=build_problem(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            title="Validation error",
            detail=detail,
            instance=str(request.url.path),
            problem_type="https://smart-campus.local/problems/validation-error",
        ),
        media_type="application/problem+json",
    )

def verify_bearer_token(authorization: Optional[str] = Header(default=None)) -> None:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=build_problem(
                status_code=status.HTTP_401_UNAUTHORIZED,
                title="Unauthorized",
                detail="Missing Authorization header",
                problem_type="https://smart-campus.local/problems/unauthorized",
            ),
        )

    expected = f"Bearer {AUTH_TOKEN}"
    if authorization != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=build_problem(
                status_code=status.HTTP_401_UNAUTHORIZED,
                title="Unauthorized",
                detail="Invalid bearer token",
                problem_type="https://smart-campus.local/problems/unauthorized",
            ),
        )

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=SERVICE_NAME,
        version=SERVICE_VERSION,
    )

@app.post(
    "/cameras",
    response_model=CameraInfo,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_bearer_token)],
    responses={
        401: {"model": ProblemDetails},
        422: {"model": ProblemDetails},
    },
)
def register_camera(payload: CameraRegister, response: Response) -> CameraInfo:
    if payload.fps >= 60:
        response.headers["X-Warning"] = "maximum-fps-reached"

    camera_id = payload.camera_id
    camera = {
        "camera_id": camera_id,
        "location": payload.location,
        "resolution": payload.resolution,
        "fps": payload.fps,
        "status": CameraStatusEnum.active,
        "registered_at": now_iso()
    }
    CAMERAS[camera_id] = camera

    return CameraInfo(**camera)

@app.get(
    "/cameras",
    response_model=List[CameraInfo],
    dependencies=[Depends(verify_bearer_token)],
    responses={
        401: {"model": ProblemDetails},
    },
)
def list_cameras() -> List[CameraInfo]:
    return [CameraInfo(**cam) for cam in CAMERAS.values()]

@app.get(
    "/cameras/{camera_id}",
    response_model=CameraInfo,
    dependencies=[Depends(verify_bearer_token)],
    responses={
        401: {"model": ProblemDetails},
        404: {"model": ProblemDetails},
    },
)
def get_camera(camera_id: str) -> CameraInfo:
    if camera_id not in CAMERAS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=build_problem(
                status_code=status.HTTP_404_NOT_FOUND,
                title="Not Found",
                detail=f"Camera {camera_id} does not exist",
                instance=f"/cameras/{camera_id}",
                problem_type="https://smart-campus.local/problems/not-found",
            ),
        )
    return CameraInfo(**CAMERAS[camera_id])

@app.get(
    "/cameras/{camera_id}/frame",
    dependencies=[Depends(verify_bearer_token)],
    responses={
        200: {
            "content": {"image/jpeg": {}},
            "description": "Returns a raw JPEG image frame from the mock camera stream.",
        },
        401: {"model": ProblemDetails},
        404: {"model": ProblemDetails},
    },
)
def get_camera_frame(camera_id: str) -> Response:
    if camera_id not in CAMERAS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=build_problem(
                status_code=status.HTTP_404_NOT_FOUND,
                title="Not Found",
                detail=f"Camera {camera_id} does not exist",
                instance=f"/cameras/{camera_id}/frame",
                problem_type="https://smart-campus.local/problems/not-found",
            ),
        )

    # Use OpenCV to generate a mock image frame
    # Create a 640x480 dark blue background
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    img[:, :] = [70, 30, 30]  # BGR format (dark red/blue)

    # Draw camera info and timestamp on frame
    current_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    location = CAMERAS[camera_id]["location"]
    resolution = CAMERAS[camera_id]["resolution"].value

    cv2.putText(img, f"STREAM: {camera_id}", (30, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (255, 255, 255), 2)
    cv2.putText(img, f"Location: {location}", (30, 140), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (200, 200, 200), 2)
    cv2.putText(img, f"Resolution: {resolution}", (30, 200), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (200, 200, 200), 2)
    cv2.putText(img, f"FPS: {CAMERAS[camera_id]['fps']}", (30, 260), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (200, 200, 200), 2)
    cv2.putText(img, current_time, (30, 420), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 1)

    # Encode image as JPEG
    success, encoded_image = cv2.imencode(".jpg", img)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to encode video frame",
        )

    return Response(content=encoded_image.tobytes(), media_type="image/jpeg")
