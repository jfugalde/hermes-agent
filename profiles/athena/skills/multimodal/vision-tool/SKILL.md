---
name: vision-tool
description: Vision tool for image analysis with Ollama models.
category: multimodal
---

# Vision Tool Skill

Provides the `vision_analyze` tool for analyzing images using a vision-capable LLM (e.g., qwen2.5vl:7b via Ollama).

## Tools Provided

- `vision_analyze`: Analyze an image from a URL or base64 data and answer a question about it.

## Usage

From an agent or skill, you can call the tool via the Hermes tool interface (if exposed) or use the provided Python helper.

### Python Helper

The skill includes a Python module `vision_tool.py` with a function:

```python
def vision_analyze(image_url: str, question: str = "Describe this image in detail.") -> str:
    """
    Analyze an image using the configured vision model.
    Returns a textual description.
    """
```

### Configuration

The skill expects the following environment variables to be set:

- `OLLAMA_VISION_MODEL`: The model to use for vision (default: `qwen2.5vl:7b`).
- `OLLAMA_API_URL`: The base URL of the Ollama API (default: `http://host.docker.internal:11434/v1`).

These are typically already set in the Athena profile's config.yaml under the `ollama-vision` provider.

## Implementation Details

The tool sends a request to the Ollama `/api/generate` endpoint with the image encoded as base64 and a prompt containing the question.

## Example

```python
from vision_tool import vision_analyze
desc = vision_analyze("https://example.com/image.jpg", "What is the main subject?")
print(desc)
```

## Notes

- Requires network access to the Ollama instance.
- The model must support vision (e.g., `llava`, `bakllava`, `qwen2.5vl`).
- Errors are returned as strings starting with `"Error:"`.

---
*Skill created for the Athena Hermes profile to enable image analysis capabilities.*