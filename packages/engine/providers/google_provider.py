"""
GOOGLE GEMINI PROVIDER
======================

Purpose: Adapter for Google Gemini API (gemini-1.5-flash, gemini-1.5-pro)
Strategy: Convert OpenAI format -> Google format -> Execute -> Convert back to OpenAI format

OpenAI compatibility:
- Streaming outputs OpenAI-compatible SSE format
- Finish reasons mapped (stop, length, content_filter)
- Imports aligned with base.py

MOCK MODE (SIMULATOR):
    If API key starts with 'goog-test', returns mock responses without making real API calls.
    This enables local testing without spending money or requiring real API keys.

Key Differences from OpenAI:
1. System prompts: Go into model's `system_instruction`, not message history
2. Message roles: 'assistant' -> 'model', 'user' -> 'user'
3. Content structure: 'content' -> 'parts' array
4. Safety settings: Must be configured to avoid over-blocking

Model notes:
- Gemini 1.5 Flash: low per-token price
- Gemini 1.5 Pro: 1M token context
"""

import os
import re
import json
import time
import asyncio
import uuid
import structlog
from typing import Dict, List, Optional, Any, AsyncGenerator
from decimal import Decimal
import httpx

try:
    import google.generativeai as genai
    from google.generativeai.types import GenerationConfig, HarmCategory, HarmBlockThreshold
except ImportError:
    genai = None
    GenerationConfig = None
    HarmCategory = None
    HarmBlockThreshold = None

from .base import LLMProvider

log = structlog.get_logger()


# ============================================================================
# MOCK MODE CONFIGURATION
# ============================================================================

def is_mock_google_key(api_key: str) -> bool:
    """Check if this is a mock/test Google API key."""
    return api_key.startswith("goog-test")


def generate_mock_google_response(model: str, messages: list) -> Dict[str, Any]:
    """
    Generate a mock Google Gemini response (in OpenAI format).
    
    Returns a properly structured response that matches what complete() would return.
    """
    # Estimate token counts based on message content
    total_input_chars = sum(len(str(m.get("content", ""))) for m in messages)
    input_tokens = max(10, total_input_chars // 4)  # ~4 chars per token
    output_tokens = 15  # Fixed mock output length
    
    return {
        "id": f"chatcmpl-mock-gemini-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": f"[MOCK GEMINI RESPONSE] This is a simulated response for model {model}. Your request was processed successfully in mock mode."
                },
                "finish_reason": "stop"
            }
        ],
        "usage": {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens
        },
        # Custom field to indicate mock mode
        "_mock": True
    }


async def generate_mock_google_stream(model: str, messages: list) -> AsyncGenerator[str, None]:
    """
    Generate a mock Google Gemini streaming response (in OpenAI format).
    
    Yields SSE chunks in OpenAI format.
    """
    chunk_id = f"chatcmpl-mock-gemini-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    
    # Mock response text split into chunks
    mock_text = f"[MOCK GEMINI STREAM] Simulated streaming response for {model}."
    chunks = [mock_text[i:i+5] for i in range(0, len(mock_text), 5)]
    
    # Initial chunk with role
    initial_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
    }
    yield f"data: {json.dumps(initial_chunk)}\n\n"
    
    # Content chunks
    for chunk_text in chunks:
        content_chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": chunk_text}, "finish_reason": None}]
        }
        yield f"data: {json.dumps(content_chunk)}\n\n"
    
    # Final chunk
    final_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]
    }
    yield f"data: {json.dumps(final_chunk)}\n\n"
    yield "data: [DONE]\n\n"

# ============================================================================
# GOOGLE API KEY VALIDATION
# ============================================================================

GOOGLE_API_KEY_REGEX = re.compile(r'^AIza[0-9A-Za-z_-]{35}$')

def validate_google_key_format(key: str) -> bool:
    """
    Validate Google API key format.
    
    Format: AIza[35 alphanumeric/underscore/hyphen characters]
    Example: AIzaSyD1234567890abcdefghijklmnopqrstu
    
    Returns:
        True if format is valid, False otherwise
    """
    return bool(GOOGLE_API_KEY_REGEX.match(key))

# ============================================================================
# GOOGLE GEMINI PROVIDER
# ============================================================================

class GoogleProvider(LLMProvider):
    """
    Google Gemini API adapter for Oxy Engine.
    
    Converts OpenAI-format requests to Google Gemini format and back.
    Handles system instructions, role mapping, and safety settings.
    
    OpenAI compatibility:
    - stream() returns OpenAI-compatible SSE strings
    - complete() properly maps Gemini finish_reason to OpenAI format
    - All imports aligned with base.py (LLMProvider only)
    """
    
    def __init__(
        self,
        api_key: str,
        http_client: httpx.AsyncClient,
        provider_name: str = "google"
    ):
        """
        Initialize Google Gemini provider.
        
        Args:
            api_key: Google AI Studio API key (format: AIza...) or mock key (goog-test...)
            http_client: Shared async HTTP client (for consistency with other providers)
            provider_name: Provider identifier (default: "google")
            
        Raises:
            ImportError: If google-generativeai library not installed (unless mock mode)
            ValueError: If API key format is invalid (unless mock mode)
        """
        super().__init__(api_key, http_client, provider_name)
        
        self.api_key = api_key
        self.log = log.bind(provider="google")
        self._mock_mode = is_mock_google_key(api_key)
        
        # MOCK MODE: Skip validation and library initialization
        if self._mock_mode:
            self.log.debug("google_provider_mock_mode")
            self.safety_settings = {}
            return
        
        if genai is None:
            raise ImportError(
                "google-generativeai library not installed. "
                "Run: pip install google-generativeai"
            )
        
        if not validate_google_key_format(api_key):
            raise ValueError(
                "Invalid Google API key format. "
                "Expected format: AIza[35 characters]. "
                "Get your key from https://https://aistudio.google.com/app/api-keys"
            )
        
        genai.configure(api_key=api_key)
        
        # Safety settings: BLOCK_NONE for enterprise use cases
        # (Customers want content filtering OFF by default, they'll implement their own)
        self.safety_settings = {
            HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
            HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
        }
        
        self.log.debug("google_provider_initialized")  # built for each request: no key material, no info line
    
    def _convert_messages_to_gemini_format(
        self, 
        messages: List[Dict[str, Any]]
    ) -> tuple[Optional[str], List[Dict[str, Any]]]:
        """
        Convert OpenAI message format to Google Gemini format.
        
        OpenAI Format:
        [
          {"role": "system", "content": "You are helpful"},
          {"role": "user", "content": "Hello"},
          {"role": "assistant", "content": "Hi!"}
        ]
        
        Gemini Format:
        system_instruction = "You are helpful"
        history = [
          {"role": "user", "parts": ["Hello"]},
          {"role": "model", "parts": ["Hi!"]}
        ]
        
        CRITICAL: System messages MUST be extracted and NOT included in history.
        If left in history, Gemini will hallucinate.
        
        Returns:
            (system_instruction, history)
        """
        system_instruction = None
        history = []
        
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "")
            
            if role == "system":
                # System messages become system_instruction (separate parameter)
                if system_instruction is None:
                    system_instruction = content
                else:
                    # If multiple system messages, concatenate them
                    system_instruction += "\n\n" + content
            elif role == "user":
                history.append({
                    "role": "user",
                    "parts": [content]
                })
            elif role == "assistant":
                # OpenAI 'assistant' -> Gemini 'model'
                history.append({
                    "role": "model",
                    "parts": [content]
                })
        
        return system_instruction, history
    
    def _extract_text_from_response(self, response: Any) -> str:
        """
        Extract text from Gemini response.
        
        Handles different response formats (text, parts array).
        """
        try:
            # Primary path: response.text
            if hasattr(response, 'text'):
                return response.text
            
            # Fallback: parts array
            if hasattr(response, 'candidates') and len(response.candidates) > 0:
                candidate = response.candidates[0]
                if hasattr(candidate, 'content') and hasattr(candidate.content, 'parts'):
                    parts_text = []
                    for part in candidate.content.parts:
                        if hasattr(part, 'text'):
                            parts_text.append(part.text)
                    return ''.join(parts_text)
            
            # If all else fails
            return str(response)
        except Exception as e:
            self.log.error("failed_to_extract_text_from_gemini_response", error=str(e))
            return ""
    
    def _map_finish_reason(self, response: Any) -> str:
        """
        Map Gemini finish_reason to OpenAI format.
        
        Mapping:
        - SAFETY -> content_filter
        - MAX_TOKENS/LENGTH -> length
        - STOP -> stop
        
        Args:
            response: Gemini API response object
            
        Returns:
            OpenAI finish_reason string
        """
        finish_reason = "stop"  # Default
        
        try:
            if hasattr(response, 'candidates') and len(response.candidates) > 0:
                candidate = response.candidates[0]
                
                if hasattr(candidate, 'finish_reason'):
                    gemini_finish_reason = str(candidate.finish_reason)
                    
                    # Map Gemini reasons to OpenAI standard
                    if "SAFETY" in gemini_finish_reason:
                        finish_reason = "content_filter"
                        self.log.warning("gemini_safety_block", reason=gemini_finish_reason)
                    elif "MAX_TOKENS" in gemini_finish_reason or "LENGTH" in gemini_finish_reason:
                        finish_reason = "length"
                    elif "STOP" in gemini_finish_reason:
                        finish_reason = "stop"
                    else:
                        # Unknown finish reason, log and default to stop
                        self.log.warning("unknown_gemini_finish_reason", reason=gemini_finish_reason)
                        finish_reason = "stop"
        
        except Exception as e:
            self.log.warning("finish_reason_mapping_failed", error=str(e))
        
        return finish_reason
    
    async def complete(
        self,
        model: str,
        messages: list,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Execute non-streaming completion via Google Gemini API.
        
        Args:
            model: Gemini model name (e.g., "gemini-1.5-pro")
            messages: OpenAI-format messages
            temperature: Sampling temperature (0.0-2.0)
            max_tokens: Maximum tokens to generate
            **kwargs: Additional parameters (top_p, etc.)
            
        Returns:
            OpenAI-format completion response:
            {
                "id": "chatcmpl-...",
                "object": "chat.completion",
                "created": 1234567890,
                "model": "gemini-1.5-pro",
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "..."},
                    "finish_reason": "stop"
                }],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30
                }
            }
            
        Raises:
            HTTPException: On API errors (rate limits, blocked prompts, etc.)
            
        MOCK MODE: If API key starts with 'goog-test', returns mock response.
        """
        # =========================================================================
        # HARD MOCK: Prevent network call for test keys (MUST BE FIRST!)
        # =========================================================================
        if self.api_key and self.api_key.startswith("goog-test"):
            self.log.info("google_hard_mock", model=model, key_prefix=self.api_key[:12])
            return {
                "id": "mock-gemini-hard",
                "object": "chat.completion",
                "created": 1234567890,
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "HARD MOCK GEMINI"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}
            }
        # =========================================================================
        
        try:
            # Extract system instruction and convert messages
            system_instruction, history = self._convert_messages_to_gemini_format(messages)
            
            # Get last user message for generation (Gemini expects this separate from history)
            if len(history) == 0:
                raise ValueError("No messages found in request")
            
            # If history has only one message, no chat history needed
            if len(history) == 1:
                user_message = history[0]["parts"][0]
                chat_history = []
            else:
                # Last message is the user prompt, rest is history
                user_message = history[-1]["parts"][0]
                chat_history = history[:-1]
            
            # Initialize model
            model_instance = genai.GenerativeModel(
                model_name=model,
                system_instruction=system_instruction,
                safety_settings=self.safety_settings
            )
            
            # Create generation config
            generation_config = GenerationConfig(
                temperature=temperature or 0.7,
                max_output_tokens=max_tokens or 4096,
                top_p=kwargs.get("top_p", 1.0),
            )
            
            # Execute completion
            if chat_history:
                # Multi-turn conversation
                chat = model_instance.start_chat(history=chat_history)
                response = await asyncio.to_thread(
                    chat.send_message,
                    user_message,
                    generation_config=generation_config
                )
            else:
                # Single-turn generation
                response = await asyncio.to_thread(
                    model_instance.generate_content,
                    user_message,
                    generation_config=generation_config
                )
            
            # Extract response text
            response_text = self._extract_text_from_response(response)
            
            # Map finish_reason to the OpenAI format
            finish_reason = self._map_finish_reason(response)
            
            # Extract token usage
            input_tokens = 0
            output_tokens = 0
            
            if hasattr(response, 'usage_metadata'):
                input_tokens = getattr(response.usage_metadata, 'prompt_token_count', 0)
                output_tokens = getattr(response.usage_metadata, 'candidates_token_count', 0)
            
            # Calculate cost using pricing engine
            from .pricing import PricingEngine
            cost = PricingEngine.calculate_google_cost(model, input_tokens, output_tokens)
            
            self.log.info(
                "google_completion_success",
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=float(cost),
                finish_reason=finish_reason
            )
            
            # Return OpenAI-compatible response
            return {
                "id": f"chatcmpl-gemini-{os.urandom(12).hex()}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": response_text
                    },
                    "finish_reason": finish_reason
                }],
                "usage": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens
                }
            }
            
        except Exception as e:
            error_msg = str(e)
            self.log.error(
                "google_completion_failed",
                model=model,
                error=error_msg,
                exc_info=True
            )
            
            # Handle specific Google API errors
            if "API_KEY_INVALID" in error_msg or "invalid_api_key" in error_msg.lower():
                from fastapi import HTTPException
                raise HTTPException(
                    status_code=401,
                    detail="Invalid Google API key. Get your key from https://https://aistudio.google.com/app/api-keys"
                )
            
            if "quota" in error_msg.lower() or "rate_limit" in error_msg.lower():
                from fastapi import HTTPException
                raise HTTPException(
                    status_code=429,
                    detail="Google API rate limit exceeded. Please try again later."
                )
            
            if "blocked" in error_msg.lower():
                from fastapi import HTTPException
                raise HTTPException(
                    status_code=400,
                    detail="Content blocked by Google's safety filters. Try rephrasing your prompt."
                )
            
            # Generic error
            from fastapi import HTTPException
            raise HTTPException(
                status_code=500,
                detail=f"Google Gemini API error: {error_msg}"
            )
    
    async def stream(
        self,
        model: str,
        messages: list,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        **kwargs
    ) -> AsyncGenerator[str, None]:
        """
        Execute streaming completion via Google Gemini API.
        
        Output:
        - Yields OpenAI-compatible SSE strings: "data: {json}\n\n"
        - Includes role initialization chunk
        - Includes [DONE] marker
        - Proper error handling with SSE format
        
        MOCK MODE: If API key starts with 'goog-test', returns mock stream.
        
        Args:
            model: Gemini model name
            messages: OpenAI-format messages
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate
            **kwargs: Additional parameters
            
        Yields:
            OpenAI-format SSE strings:
            "data: {chunk}\n\n"
            ...
            "data: [DONE]\n\n"
        """
        # MOCK MODE: Return simulated stream for test keys
        if self._mock_mode:
            self.log.info("google_mock_stream", model=model, key_prefix=self.api_key[:12])
            async for chunk in generate_mock_google_stream(model, messages):
                yield chunk
            return
        
        try:
            # Extract system instruction and convert messages
            system_instruction, history = self._convert_messages_to_gemini_format(messages)
            
            # Get last user message
            if len(history) == 0:
                raise ValueError("No messages found in request")
            
            if len(history) == 1:
                user_message = history[0]["parts"][0]
                chat_history = []
            else:
                user_message = history[-1]["parts"][0]
                chat_history = history[:-1]
            
            # Initialize model
            model_instance = genai.GenerativeModel(
                model_name=model,
                system_instruction=system_instruction,
                safety_settings=self.safety_settings
            )
            
            # Create generation config
            generation_config = GenerationConfig(
                temperature=temperature or 0.7,
                max_output_tokens=max_tokens or 4096,
                top_p=kwargs.get("top_p", 1.0),
            )
            
            # Execute streaming completion
            if chat_history:
                chat = model_instance.start_chat(history=chat_history)
                response_stream = chat.send_message(
                    user_message,
                    generation_config=generation_config,
                    stream=True
                )
            else:
                response_stream = model_instance.generate_content(
                    user_message,
                    generation_config=generation_config,
                    stream=True
                )
            
            # Generate unique IDs for OpenAI format
            chunk_id = f"chatcmpl-gemini-{os.urandom(8).hex()}"
            created_timestamp = int(time.time())
            
            # Track whether we've sent the role chunk
            first_chunk_sent = False
            
            # Stream chunks in OpenAI format
            for chunk in response_stream:
                try:
                    # Extract text from chunk
                    chunk_text = self._extract_text_from_response(chunk)
                    
                    # Send role initialization on first chunk (OpenAI standard)
                    if not first_chunk_sent:
                        openai_chunk = {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created_timestamp,
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {"role": "assistant"},
                                "finish_reason": None
                            }]
                        }
                        yield f"data: {json.dumps(openai_chunk)}\n\n"
                        first_chunk_sent = True
                    
                    # Send content chunk
                    if chunk_text:
                        openai_chunk = {
                            "id": chunk_id,
                            "object": "chat.completion.chunk",
                            "created": created_timestamp,
                            "model": model,
                            "choices": [{
                                "index": 0,
                                "delta": {"content": chunk_text},
                                "finish_reason": None
                            }]
                        }
                        yield f"data: {json.dumps(openai_chunk)}\n\n"
                
                except Exception as e:
                    self.log.warning("google_stream_chunk_error", error=str(e))
                    continue
            
            # Send final chunk with finish_reason (OpenAI standard)
            openai_chunk = {
                "id": chunk_id,
                "object": "chat.completion.chunk",
                "created": created_timestamp,
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop"
                }]
            }
            yield f"data: {json.dumps(openai_chunk)}\n\n"
            
            # Send [DONE] marker (OpenAI standard)
            yield "data: [DONE]\n\n"
            
            self.log.info("google_stream_complete", model=model)
            
        except Exception as e:
            error_msg = str(e)
            self.log.error(
                "google_stream_failed",
                model=model,
                error=error_msg,
                exc_info=True
            )
            
            # Send error as OpenAI-format chunk
            error_chunk = {
                "id": f"chatcmpl-error-{os.urandom(8).hex()}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {"content": f"[ERROR: {error_msg}]"},
                    "finish_reason": "error"
                }]
            }
            yield f"data: {json.dumps(error_chunk)}\n\n"
            yield "data: [DONE]\n\n"
    
    def get_pricing(self, model: str) -> Dict[str, Decimal]:
        """Get Google Gemini pricing from centralized pricing engine."""
        from .pricing import PricingEngine
        return PricingEngine.get_pricing(model)
    
    def calculate_cost(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int
    ) -> Decimal:
        """Calculate cost using centralized pricing engine."""
        from .pricing import PricingEngine
        return PricingEngine.calculate_google_cost(model, prompt_tokens, completion_tokens)
    
    def get_supported_models(self) -> list[str]:
        """Get list of Google Gemini models."""
        from .pricing import PricingEngine
        return PricingEngine.get_models_by_provider("google")
    
    async def validate_key(self) -> bool:
        """
        Validate Google API key by making a test request.
        
        Returns:
            True if key is valid, False otherwise
        """
        try:
            model = genai.GenerativeModel(model_name="gemini-1.5-flash")
            
            # Simple test generation
            response = await asyncio.to_thread(
                model.generate_content,
                "Hello",
                generation_config=GenerationConfig(max_output_tokens=10)
            )
            
            # If we get here without exception, key is valid
            return True
            
        except Exception as e:
            self.log.warning("google_key_validation_failed", error=str(e))
            return False


# Factory function for easy instantiation
def create_google_provider(
    api_key: str,
    http_client: httpx.AsyncClient
) -> GoogleProvider:
    """
    Factory function to create Google provider instance.
    
    Args:
        api_key: Google API key (starts with "AIza")
        http_client: Async HTTP client
    
    Returns:
        GoogleProvider instance
    """
    return GoogleProvider(
        api_key=api_key,
        http_client=http_client,
        provider_name="google"
    )
