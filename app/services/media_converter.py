import logging
import os
import tempfile
from typing import Any, Dict, Tuple

import cv2
from moviepy.editor import VideoFileClip
from PIL import Image

from app.config import settings
from app.models.media import MediaType

logger = logging.getLogger(__name__)


class MediaConverter:
    """
    Service for converting media files to standardized formats.
    """

    def __init__(self):
        """Initialize media converter with configured limits."""
        self.max_video_length = settings.MAX_VIDEO_LENGTH_SECONDS
        self.upload_dir = settings.UPLOAD_DIR
        self.processed_dir = settings.PROCESSED_DIR

    def process_media(self, file_path: str, media_type: MediaType) -> Tuple[str, Dict[str, Any]]:
        """
        Process a media file by converting it to the appropriate format.

        Args:
            file_path: Path to input media file
            media_type: Type of media (IMAGE, VIDEO, GIF)

        Returns:
            Tuple of (output_path, metadata)
        """
        try:
            # Ensure output directory exists
            os.makedirs(self.processed_dir, exist_ok=True)

            # Generate output filename
            base_name = os.path.basename(file_path)
            file_name, _ = os.path.splitext(base_name)

            if media_type == MediaType.IMAGE:
                output_path = os.path.join(self.processed_dir, f"{file_name}.png")
                metadata = self.convert_image(file_path, output_path)
                return output_path, metadata
            if media_type in [MediaType.VIDEO, MediaType.GIF]:
                output_path = os.path.join(self.processed_dir, f"{file_name}.mp4")
                metadata = self.convert_video(file_path, output_path)
                return output_path, metadata
        except Exception as e:
            logger.error(f"Error processing media: {e}")
            raise

    def convert_image(self, input_path: str, output_path: str) -> Dict[str, Any]:
        """
        Convert an image to PNG format.

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
                "channels": len(img.getbands()),
                "aspect_ratio": round(width / height, 2) if height else 0,
            }

        except Exception as e:
            logger.error(f"Error converting image: {e}")
            raise Exception(f"Error converting image: {str(e)}") from e

    def convert_video(self, input_path: str, output_path: str) -> Dict[str, Any]:
        """
        Convert a video to MP4 format and limit to max length.

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
            trimmed = False
            if duration > self.max_video_length:
                clip = clip.subclip(0, self.max_video_length)
                duration = self.max_video_length
                trimmed = True

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

            # Extract frame for thumbnail
            thumbnail_path = output_path.replace(".mp4", "_thumb.jpg")
            clip.save_frame(thumbnail_path, t=1)  # Save frame at 1 second

            # Get additional metadata using OpenCV
            cap = cv2.VideoCapture(output_path)
            fps = cap.get(cv2.CAP_PROP_FPS)
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            cap.release()

            # Extract metadata
            metadata = {
                "width": clip.w,
                "height": clip.h,
                "duration": duration,
                "original_duration": clip.duration,
                "trimmed": trimmed,
                "fps": fps,
                "frame_count": frame_count,
                "format": "MP4",
                "has_audio": clip.audio is not None,
                "aspect_ratio": round(clip.w / clip.h, 2) if clip.h else 0,
                "thumbnail": os.path.basename(thumbnail_path),
            }

            # Close clip to free resources
            clip.close()

            return metadata
        except Exception as e:
            logger.error(f"Error converting video: {e}")
            raise Exception(f"Error converting video: {str(e)}") from e

    def validate_file(self, file_path: str, media_type: MediaType) -> bool:
        """
        Validate that a file is of the expected media type and can be processed.

        Args:
            file_path: Path to the file
            media_type: Expected media type

        Returns:
            True if valid, False otherwise
        """
        try:
            if media_type == MediaType.IMAGE:
                Image.open(file_path).verify()
                return True
            if media_type in [MediaType.VIDEO, MediaType.GIF]:
                clip = VideoFileClip(file_path)
                clip.close()
                return True
            return False
        except Exception as e:
            logger.error(f"File validation failed: {e}")
            return False
