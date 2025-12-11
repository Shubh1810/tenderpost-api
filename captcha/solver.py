"""2Captcha API v2 integration for CAPTCHA solving."""

import asyncio
import os
from typing import Dict, Optional

import httpx


# 2Captcha API v2 Endpoints
CREATE_TASK_URL = "https://api.2captcha.com/createTask"
GET_RESULT_URL = "https://api.2captcha.com/getTaskResult"

# Polling configuration
POLL_INTERVAL = 5  # seconds
MAX_WAIT_TIME = 120  # seconds


def log_step(step: str, status: str, details: Dict[str, any]) -> None:
    """Print detailed logs for each pipeline step."""
    status_emoji = {
        "success": "✅",
        "error": "❌",
        "warning": "⚠️",
        "initiated": "🔵",
        "processing": "⏳",
    }.get(status, "📌")

    print(f"{status_emoji} [{step}] {status.upper()}")
    for key, value in details.items():
        print(f"   • {key}: {value}")


async def test_2captcha_connectivity() -> bool:
    """
    Test connectivity to 2Captcha service.
    Returns True if service is reachable, False otherwise.
    """
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get("https://2captcha.com")
            if response.status_code == 200:
                log_step(
                    "2Captcha Connectivity",
                    "success",
                    {"status": "Service reachable", "code": response.status_code},
                )
                return True
            else:
                log_step(
                    "2Captcha Connectivity",
                    "warning",
                    {"status": "Unexpected response", "code": response.status_code},
                )
                return False
    except httpx.TimeoutException:
        log_step(
            "2Captcha Connectivity",
            "error",
            {"error": "Timeout", "message": "Cannot reach 2captcha.com - possible Cloudflare outage"},
        )
        return False
    except Exception as e:
        log_step("2Captcha Connectivity", "error", {"error": str(e)})
        return False


async def solve_captcha_2captcha(
    image_base64: str, api_key: Optional[str] = None
) -> Dict[str, any]:
    """
    Solve CAPTCHA using 2Captcha API v2 (ImageToTextTask).

    Args:
        image_base64: Base64-encoded CAPTCHA image (without data:image/png;base64, prefix)
        api_key: 2Captcha API key (defaults to TWOCAPTCHA_API_KEY env var)

    Returns:
        {
            "success": bool,
            "solution": str (if success=True),
            "error": str (if success=False),
            "task_id": str,
            "elapsed_time": float
        }
    """
    if api_key is None:
        api_key = os.getenv("TWOCAPTCHA_API_KEY")

    if not api_key:
        log_step(
            "2Captcha Solver",
            "error",
            {"error": "API key not found", "message": "Set TWOCAPTCHA_API_KEY environment variable"},
        )
        return {"success": False, "error": "API key not provided"}

    # Clean base64 string (remove data URI prefix if present)
    if "," in image_base64:
        image_base64 = image_base64.split(",", 1)[1]

    # Step 1: Create task
    log_step("2Captcha Solver", "initiated", {"action": "Creating task"})

    task_payload = {
        "clientKey": api_key,
        "task": {
            "type": "ImageToTextTask",
            "body": image_base64,
            "phrase": False,
            "case": False,
            "numeric": 0,  # 0=no preference, 1=only numbers, 2=only letters
            "math": False,
            "minLength": 1,
            "maxLength": 8,
            "comment": "Enter the text you see in the captcha image",
        },
        "languagePool": "en",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Create task
            create_response = await client.post(CREATE_TASK_URL, json=task_payload)
            create_data = create_response.json()

            if create_data.get("errorId", 0) != 0:
                error_msg = create_data.get("errorDescription", "Unknown error")
                log_step("2Captcha Solver", "error", {"error": error_msg})
                return {"success": False, "error": error_msg}

            task_id = create_data.get("taskId")
            if not task_id:
                log_step("2Captcha Solver", "error", {"error": "No task ID received"})
                return {"success": False, "error": "No task ID received"}

            log_step("2Captcha Solver", "processing", {"task_id": task_id})

            # Step 2: Poll for result
            result_payload = {"clientKey": api_key, "taskId": task_id}

            start_time = asyncio.get_event_loop().time()
            elapsed = 0

            while elapsed < MAX_WAIT_TIME:
                await asyncio.sleep(POLL_INTERVAL)
                elapsed = asyncio.get_event_loop().time() - start_time

                result_response = await client.post(GET_RESULT_URL, json=result_payload)
                result_data = result_response.json()

                # Check for errors
                if result_data.get("errorId", 0) != 0:
                    error_msg = result_data.get("errorDescription", "Unknown error")
                    log_step("2Captcha Solver", "error", {"error": error_msg, "elapsed": f"{elapsed:.1f}s"})
                    return {"success": False, "error": error_msg, "task_id": task_id}

                # Check if ready
                if result_data.get("status") == "ready":
                    solution = result_data.get("solution", {}).get("text", "")

                    # Validate solution
                    if not solution or len(solution) < 1:
                        log_step(
                            "2Captcha Solver",
                            "error",
                            {"error": "Empty solution received", "elapsed": f"{elapsed:.1f}s"},
                        )
                        return {
                            "success": False,
                            "error": "Empty solution",
                            "task_id": task_id,
                            "elapsed_time": elapsed,
                        }

                    log_step(
                        "2Captcha Solver",
                        "success",
                        {
                            "solution": solution,
                            "length": len(solution),
                            "elapsed": f"{elapsed:.1f}s",
                        },
                    )

                    return {
                        "success": True,
                        "solution": solution,
                        "task_id": task_id,
                        "elapsed_time": elapsed,
                    }

            # Timeout
            log_step(
                "2Captcha Solver",
                "error",
                {"error": "Timeout", "max_wait": f"{MAX_WAIT_TIME}s"},
            )
            return {
                "success": False,
                "error": "Timeout waiting for solution",
                "task_id": task_id,
                "elapsed_time": elapsed,
            }

    except httpx.TimeoutException:
        log_step("2Captcha Solver", "error", {"error": "HTTP timeout"})
        return {"success": False, "error": "HTTP request timeout"}
    except httpx.RequestError as e:
        log_step("2Captcha Solver", "error", {"error": f"Network error: {str(e)}"})
        return {"success": False, "error": f"Network error: {str(e)}"}
    except Exception as e:
        log_step("2Captcha Solver", "error", {"error": str(e)})
        return {"success": False, "error": str(e)}

