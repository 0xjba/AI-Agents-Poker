import requests
import json
import os
from typing import List, Dict
import logging

logger = logging.getLogger(__name__)

class OpenRouterClient:
    def __init__(self, model_name: str = "anthropic/claude-2"):
        self.api_key = os.getenv("OPENROUTER_API_KEY")
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY environment variable not set")
        
        self.model = model_name
        self.base_url = "https://api.red-pill.ai/v1"
        
        self.headers = {
            "Authorization": f"Bearer {self.api_key}",
            "HTTP-Referer": os.getenv("OPENROUTER_REFERRER", "http://localhost"),
            "X-Title": "Poker Agent"
        }

    def get_available_models(self):
        """Fetch available models from OpenRouter"""
        try:
            response = requests.get(
                f"{self.base_url}/models",
                headers=self.headers
            )
            response.raise_for_status()
            data = response.json()
            # Extract model data from response
            return [
                {
                    "id": model["id"],
                    "name": model.get("name", model["id"]),
                    "context_length": model.get("context_length", 4096)
                }
                for model in data.get("data", [])
            ]
        except Exception as e:
            logger.error(f"Failed to fetch models: {e}")
            return []

    def get_completion(self, messages: List[Dict]) -> str:
        """Get completion from OpenRouter with better error handling"""
        try:
            # Display informative message about API call
            logger.info(f"Calling OpenRouter API with model: {self.model}")
            
            response = requests.post(
                url=f"{self.base_url}/chat/completions",
                headers=self.headers,
                data=json.dumps({
                    "model": self.model,
                    "messages": messages
                })
            )
            
            # Check for HTTP errors
            response.raise_for_status()
            
            # Try to parse JSON response with better error handling
            try:
                result = response.json()
            except json.JSONDecodeError:
                logger.error("API returned invalid JSON response")
                logger.debug(f"Response content: {response.text[:200]}...")
                raise ValueError("API returned invalid JSON response")
                
            # Check if the response contains the choices field
            if 'choices' not in result:
                error_msg = "API response missing 'choices' field"
                logger.error(error_msg)
                logger.debug(f"Response content: {str(result)[:200]}...")
                
                # Check for error message in the response
                if 'error' in result:
                    error_detail = result.get('error', {}).get('message', 'Unknown error')
                    logger.error(f"API error: {error_detail}")
                    raise ValueError(f"API error: {error_detail}")
                    
                raise ValueError(error_msg)
                
            # Success path
            return result['choices'][0]['message']['content']
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error: {e}")
            raise ValueError(f"API request failed: {str(e)}")
            
        except Exception as e:
            logger.error(f"Failed to get completion: {e}")
            raise