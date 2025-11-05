import base64
import os
import time
from typing import Dict, List, Optional, Tuple

import boto3
from io import BytesIO
from PIL import Image


TEXT_MODEL_ARN = "arn:aws:bedrock:us-east-1:127214171089:inference-profile/us.meta.llama4-scout-17b-instruct-v1:0"
IMAGE_MODEL_ARN = "arn:aws:bedrock:us-east-1:127214171089:inference-profile/us.meta.llama3-2-11b-instruct-v1:0"


def get_bedrock_client(region: str = "us-east-1"):
	# Increase timeout for image processing which can take longer
	from botocore.config import Config
	config = Config(
		read_timeout=240,  # 2 minutes for image processing
		connect_timeout=10,  # 10 seconds for connection
		retries={'max_attempts': 2}  # Retry up to 2 times
	)
	return boto3.client("bedrock-runtime", region_name=region, config=config)


def converse_text(
	bedrock,
	user_text: str,
	system_text: Optional[str] = None,
	max_tokens: int = 8192,
	temperature: float = 0.0,
	top_p: float = 0.9,
) -> str:
	messages = [
		{
			"role": "user",
			"content": [
				{"text": user_text.strip()},
			]
		}
	]

	system = None
	if system_text:
		system = [{"text": system_text.strip()}]
	else:
		system = []

	response = bedrock.converse(
		modelId=TEXT_MODEL_ARN,
		messages=messages,
		system=system,
		inferenceConfig={
			"maxTokens": max_tokens,
			"temperature": temperature,
			"topP": top_p,
		},
	)

	# Bedrock converse returns a structured output; collect text parts
	outputs: List[str] = []
	for content_block in response.get("output", {}).get("message", {}).get("content", []):
		if "text" in content_block:
			outputs.append(content_block["text"]) 
	return "\n".join(outputs).strip()



def _shrink_image_to_max_pixels(image_bytes: bytes, image_format: str, max_total_pixels: int = 2000000) -> Tuple[str, bytes]:
	"""Resize the image so width*height <= max_total_pixels AND max(width,height) <= 1120, preserving aspect ratio.
	Returns (format, bytes). Converts 'jpg' to 'jpeg' and ensures PNG/JPEG output.
	"""
	fmt = (image_format or "").lower()
	if fmt == "jpg":
		fmt = "jpeg"

	MAX_DIMENSION = 1120

	with Image.open(BytesIO(image_bytes)) as img:
		width, height = img.size
		total = width * height

		# Compute scale for total pixels and max dimension
		scales: List[float] = []
		if total > max_total_pixels:
			scales.append((max_total_pixels / float(total)) ** 0.5)
		if max(width, height) > MAX_DIMENSION:
			scales.append(MAX_DIMENSION / float(max(width, height)))

		scale = min(scales) if scales else 1.0
		if scale < 1.0:
			new_w = max(1, int(width * scale))
			new_h = max(1, int(height * scale))
			resized = img.resize((new_w, new_h), Image.LANCZOS)
		else:
			resized = img

		out_fmt = "PNG" if fmt == "png" else "JPEG"
		buf = BytesIO()
		if out_fmt == "PNG":
			resized.save(buf, format=out_fmt, optimize=True)
		else:
			resized = resized.convert("RGB")
			resized.save(buf, format=out_fmt, optimize=True, quality=90)
		return ("png" if out_fmt == "PNG" else "jpeg", buf.getvalue())


def converse_image(
	bedrock,
	image_bytes: bytes,
	image_format: str,
	prompt: str,
	max_tokens: int = 8192,  # Increased from 2048 to handle large tables
	temperature: float = 0.0,
	top_p: float = 0.1,
) -> str:
	# Resize if needed to meet model pixel limits
	safe_fmt, safe_bytes = _shrink_image_to_max_pixels(image_bytes, image_format)

	messages = [
		{
			"role": "user",
			"content": [
				{"text": prompt},
				{
					"image": {
						"format": safe_fmt,
						"source": {"bytes": safe_bytes},
					}
				}
			]
		}
	]

	response = bedrock.converse(
		modelId=IMAGE_MODEL_ARN,
		messages=messages,
		inferenceConfig={
			"maxTokens": max_tokens,
			"temperature": temperature,
			"topP": top_p,
		},
	)

	outputs: List[str] = []
	for content_block in response.get("output", {}).get("message", {}).get("content", []):
		if "text" in content_block:
			outputs.append(content_block["text"]) 
	return "\n".join(outputs).strip()
