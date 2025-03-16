import json
import os
import re
import tempfile
from typing import Any, Dict

import requests
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from moviepy import VideoFileClip
from PIL import Image

from app.config import settings


class MediaProcessor:
    """Media processor for handling image and video files."""

    def __init__(self):
        """Initialize the media processor."""
        self.service_account_key = settings.GOOGLE_SERVICE_ACCOUNT_KEY
        self.gemini_api_endpoint = settings.GEMINI_API_ENDPOINT
        self.max_video_length = settings.MAX_VIDEO_LENGTH_SECONDS

    def convert_image(self, input_path: str, output_path: str) -> Dict[str, Any]:
        """
        Convert image to PNG format.

        Args:
            input_path: Path to input image
            output_path: Path to output PNG image

        Returns:
            Dictionary with metadata
        """
        try:
            img = Image.open(input_path)
            img.save(output_path, "PNG")

            # Extract metadata
            width, height = img.size
            return {
                "width": width,
                "height": height,
                "format": "PNG",
                "mode": img.mode,
                "original_format": img.format,
            }

        except Exception as e:
            raise Exception(f"Error converting image: {str(e)}") from e

    def convert_video(self, input_path: str, output_path: str) -> Dict[str, Any]:
        """
        Convert video to MP4 format and limit to max length.

        Args:
            input_path: Path to input video
            output_path: Path to output MP4 video

        Returns:
            Dictionary with metadata
        """
        try:
            # Load the video file
            clip = VideoFileClip(input_path)

            # Trim if longer than max length
            duration = clip.duration
            if duration > self.max_video_length:
                clip = clip.subclip(0, self.max_video_length)
                duration = self.max_video_length

            # Save as MP4
            clip.write_videofile(
                output_path,
                codec="libx264",
                audio_codec="aac",
                temp_audiofile=os.path.join(tempfile.gettempdir(), "temp-audio.m4a"),
                remove_temp=True,
                threads=4,
                preset="fast",
            )

            # Extract metadata
            metadata = {
                "width": clip.w,
                "height": clip.h,
                "duration": duration,
                "fps": clip.fps,
                "format": "MP4",
                "has_audio": clip.audio is not None,
            }

            # Close clip to free resources
            clip.close()

            return metadata
        except Exception as e:
            raise Exception(f"Error converting video: {str(e)}") from e

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
            raise Exception(f"Error getting access token: {str(e)}") from e

    def upload_media_to_gemini(self, file_path: str, access_token: str) -> str:
        """
        Upload media file to Gemini API.

        Args:
            file_path: Path to media file
            access_token: Google API access token

        Returns:
            File reference for Gemini API
        """
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
                raise Exception(f"Upload failed: {response.text}")
        except Exception as e:
            raise Exception(f"Error uploading media: {str(e)}") from e

    def process_with_gemini(self, file_path: str, prompt_text: str) -> Dict[str, Any]:
        """
        Process media with Gemini API.

        Args:
            file_path: Path to media file
            prompt_text: Prompt text for Gemini

        Returns:
            Processed data including description and tags
        """
        try:
            # Get access token
            access_token = self.get_access_token()

            # Upload file
            file_reference = self.upload_media_to_gemini(file_path, access_token)

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
                self.gemini_api_endpoint,
                headers=headers,
                data=json.dumps(prompt),
            )

            if response.status_code != 200:
                raise Exception(f"Gemini API error: {response.text}") from None

            result = response.json()

            if "candidates" not in result or not result["candidates"]:
                raise Exception("No response from Gemini API") from None

            gemini_output = result["candidates"][0]["output"]

            # Extract sections and tags
            return self._parse_gemini_response(gemini_output)

        except Exception as e:
            raise Exception(f"Error processing with Gemini: {str(e)}") from e

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
                r"(?:SEARCH TAGS|TAGS|6\.\s*SEARCH):(.*?)(?:$)",
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
