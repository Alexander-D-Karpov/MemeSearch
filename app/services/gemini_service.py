import json
import logging
import re
from typing import Any, Dict, Optional

import requests
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from app.config import settings

logger = logging.getLogger(__name__)


class GeminiService:
    """
    Service for interacting with Google's Gemini API.
    """

    def __init__(self):
        """Initialize the Gemini service."""
        self.service_account_key = settings.GOOGLE_SERVICE_ACCOUNT_KEY
        self.api_endpoint = settings.GEMINI_API_ENDPOINT

    def get_access_token(self) -> str:
        """
        Get access token from Google service account.

        Returns:
            Access token
        """
        try:
            credentials = service_account.Credentials.from_service_account_file(
                self.service_account_key,
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            credentials.refresh(Request())
            return credentials.token
        except Exception as e:
            logger.error(f"Error getting access token: {e}")
            raise

    def upload_media(self, file_path: str, access_token: Optional[str] = None) -> str:
        """
        Upload media file to Gemini API.

        Args:
            file_path: Path to media file
            access_token: Google API access token (optional, will be fetched if not provided)

        Returns:
            File reference for Gemini API
        """
        if not access_token:
            access_token = self.get_access_token()

        try:
            with open(file_path, "rb") as media_file:
                media_data = media_file.read()

            upload_url = "https://gemini.googleapis.com/v1beta2/files"
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/octet-stream",
            }

            response = requests.post(upload_url, headers=headers, data=media_data)

            if response.status_code == 200:
                return response.json()["name"]
                logger.error(f"Upload failed with status {response.status_code}: {response.text}")
                raise Exception(f"Upload failed: {response.text}")
        except Exception as e:
            logger.error(f"Error uploading media: {e}")
            raise

    def analyze_media(
        self,
        file_path: str,
        prompt_text: str,
        access_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Analyze media with Gemini API.

        Args:
            file_path: Path to media file
            prompt_text: Prompt text for Gemini
            access_token: Google API access token (optional, will be fetched if not provided)

        Returns:
            Analysis result dictionary
        """
        if not access_token:
            access_token = self.get_access_token()

        try:
            # Upload file
            file_reference = self.upload_media(file_path, access_token)

            # Construct prompt
            prompt = {
                "prompt": {
                    "text": prompt_text,
                    "files": [{"file": file_reference}],
                },
            }

            # Set up headers
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }

            # Send request to Gemini API
            response = requests.post(
                self.api_endpoint,
                headers=headers,
                data=json.dumps(prompt),
            )

            if response.status_code != 200:
                logger.error(f"Gemini API error: {response.text}")
                raise Exception(f"Gemini API error: {response.text}")

            result = response.json()

            if "candidates" not in result or not result["candidates"]:
                logger.error("No response from Gemini API")
                raise Exception("No response from Gemini API")

            gemini_output = result["candidates"][0]["output"]

            # Extract sections and tags
            return self._parse_gemini_response(gemini_output)
        except Exception as e:
            logger.error(f"Error analyzing media: {e}")
            raise

    def _parse_gemini_response(self, response_text: str) -> Dict[str, Any]:
        """
        Parse the Gemini API response to extract structured data.

        Args:
            response_text: Text response from Gemini API

        Returns:
            Dictionary with extracted sections (description, ocr_text, tags)
        """
        # Default structure
        result = {
            "description": "",
            "ocr_text": "",
            "tags": [],
        }

        # Extract visual description
        visual_description_match = re.search(
            r"(?:VISUAL DESCRIPTION|1\.\s*VISUAL DESCRIPTION):(.*?)(?:(?:TEXT EXTRACTION|2\.)|$)",
            response_text,
            re.DOTALL,
        )
        if visual_description_match:
            result["description"] = visual_description_match.group(1).strip()

        # Extract OCR text
        ocr_match = re.search(
            r"(?:TEXT EXTRACTION|2\.\s*TEXT EXTRACTION):(.*?)(?:(?:VISUAL CHARACTERISTICS|CONTENT CONTEXT|3\.)|$)",
            response_text,
            re.DOTALL,
        )
        if ocr_match:
            result["ocr_text"] = ocr_match.group(1).strip()

        # Extract tags - look for JSON array format first
        tags_match = re.search(r"\[(.*?)\]", response_text)
        if tags_match:
            try:
                tags_str = "[" + tags_match.group(1) + "]"
                tags = json.loads(tags_str)
                if isinstance(tags, list):
                    result["tags"] = tags
            except json.JSONDecodeError:
                pass

        # If JSON parsing failed, try to extract comma-separated tags
        if not result["tags"]:
            tags_section_match = re.search(
                r"(?:SEARCH TAGS|TAGS|SEARCH OPTIMIZATION|6\.\s*SEARCH):(.*?)(?:$)",
                response_text,
                re.DOTALL,
            )
            if tags_section_match:
                tags_text = tags_section_match.group(1).strip()

                # Try to find a bullet or numbered list
                tag_list = re.findall(r"[-*•]\s*([^-*•\n]+)", tags_text)
                if tag_list:
                    result["tags"] = [tag.strip().strip("\"'") for tag in tag_list]
                    # Try comma-separated values as fallback
                    raw_tags = re.split(r",|\n", tags_text)
                    result["tags"] = [tag.strip().strip("\"'") for tag in raw_tags if tag.strip()]

        return result
