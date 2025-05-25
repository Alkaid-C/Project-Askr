#!/usr/bin/env python3
"""
NapCat Plugin Manager - A middleware framework for QQ chatbot development

This script serves as a bridge between NapCat (OneBot 11 implementation) and custom 
chatbot plugins, simplifying bot development to writing simple Python functions.

Architecture:
- Plugin Discovery: Automatically loads .py files from plugins directory
- Event Processing: Parses OneBot 11 events and routes to appropriate handlers
- Message History: Stores all events in organized JSON files for context-aware responses
- Response Handling: Converts plugin responses to proper OneBot 11 API calls
- Async Operations: Non-blocking history storage with retry logic

File Structure:
├── plugins/                   # Plugin directory (auto-created)
│   └── example_bot.py        # Plugin files with MANIFEST
├── MessageHistory/           # Event storage (auto-created)
│   ├── Friend/{user_id}.json # Private conversations & friend events
│   ├── Group/{group_id}.json # Group conversations & group events
│   └── Others/{event_type}.json # Meta events, requests, etc.
└── napcat_plugin_manager.py # This script

Author: Plugin Manager Development Team
Version: 1.0.0
Compatible with: OneBot 11, NapCat
Python Version: 3.12+
"""

import json
import os
import importlib
import requests
import logging
import asyncio
from concurrent.futures import ThreadPoolExecutor
from flask import Flask, request
from typing import List, Dict, Optional, Union, Any

# ============================================================================
# GLOBAL CONFIGURATION AND STATE
# ============================================================================

# Valid OneBot 11 event types based on official NapCat documentation
# These are the standardized event identifiers that plugins can register for
EVENT_TYPES_: List[str] = [
    # Message Events (post_type: "message")
    "MESSAGE_PRIVATE",       # Private messages between users
    "MESSAGE_GROUP",         # Group messages in chat rooms
    
    # Message Sent Events (post_type: "message_sent") 
    "MESSAGE_SENT_PRIVATE",  # Bot's own private messages (outgoing)
    "MESSAGE_SENT_GROUP",    # Bot's own group messages (outgoing)
    
    # Notice Events (post_type: "notice") - Various platform notifications
    "NOTICE_FRIEND_ADD",           # New friend added
    "NOTICE_FRIEND_RECALL",        # Friend recalled a message
    "NOTICE_GROUP_RECALL",         # Group message recalled
    "NOTICE_GROUP_INCREASE",       # New member joined group
    "NOTICE_GROUP_DECREASE",       # Member left/was removed from group
    "NOTICE_GROUP_ADMIN",          # Group admin status changed
    "NOTICE_GROUP_BAN",            # Group member muted/unmuted
    "NOTICE_GROUP_UPLOAD",         # File uploaded to group
    "NOTICE_GROUP_CARD",           # Group member nickname changed
    "NOTICE_GROUP_NAME",           # Group name changed
    "NOTICE_GROUP_TITLE",          # Group member title changed
    "NOTICE_POKE",                 # Someone poked someone (nudge feature)
    "NOTICE_PROFILE_LIKE",         # Profile/status liked
    "NOTICE_INPUT_STATUS",         # Typing indicator (not logged)
    "NOTICE_ESSENCE",              # Message marked as group essence
    "NOTICE_GROUP_MSG_EMOJI_LIKE", # Emoji reaction to group message
    "NOTICE_BOT_OFFLINE",          # Bot went offline
    
    # Request Events (post_type: "request") - Actions requiring approval
    "REQUEST_FRIEND",              # Friend request received
    "REQUEST_GROUP",               # Group join/invite request
    
    # Meta Events (post_type: "meta_event") - System/protocol events
    "META_HEARTBEAT",              # Keep-alive ping from NapCat
    "META_LIFECYCLE"               # NapCat lifecycle status changes
]

# Plugin registry mapping event types to handler functions
# Structure: {event_type: [function1, function2, ...]}
# Populated during initialization by scanning plugin files
PLUGIN_REGISTRY = {}  # type: Dict[str, List[callable]]

# Thread pool for async history operations (non-blocking background tasks)
# Limited to 2 workers to prevent filesystem contention
HISTORIAN_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="historian")

# System configuration dictionary
# Contains all runtime settings including server URLs, timeouts, and paths
CONFIG = {
    'NAPCAT_SERVER': {
        'api_url': 'http://localhost:3001',  # NapCat HTTP API endpoint
    },
    'NAPCAT_LISTEN': {
        'host': '0.0.0.0',                   # Bind to all interfaces
        'port': 8080                         # Port for receiving webhooks
    },
    'PATHS': {
        'plugins_dir': './plugins',          # Plugin discovery directory
        'history_dir': './MessageHistory'    # Event storage base directory
    },
    'HTTP': {
        'max_retries': 3,                    # Retry attempts for failed requests
        'timeout_seconds': 10,               # Request timeout per attempt
        'status_check_timeout': 5            # Timeout for status checks
    }
}

# Configure logging for debugging and monitoring
# INFO level provides good balance of detail vs noise
logging.basicConfig(level=logging.INFO)

# ============================================================================
# INITIALIZATION FUNCTIONS
# ============================================================================

def Initializer() -> None:
    """
    Initialize the plugin manager system by setting up directories and loading plugins.
    
    This function performs the following operations:
    1. Initializes PLUGIN_REGISTRY with empty lists for all event types
    2. Creates necessary directory structure (plugins/, MessageHistory/, etc.)
    3. Discovers and loads all plugin files from the plugins directory
    4. Validates plugin manifests and registers handler functions
    5. Logs comprehensive statistics about loaded plugins
    
    The function is designed to be idempotent - safe to call multiple times.
    Missing directories are created automatically. Invalid plugins are skipped
    with warnings, allowing the system to start even with some broken plugins.
    
    Global State Modified:
        PLUGIN_REGISTRY: Populated with event_type -> [functions] mappings
    
    Raises:
        No exceptions are raised; all errors are logged and handled gracefully
    """
    global PLUGIN_REGISTRY
    
    # Pre-populate registry with all valid event types to avoid runtime checks
    # This eliminates the need for "if key in dict" checks during event processing
    PLUGIN_REGISTRY = {eventType: [] for eventType in EVENT_TYPES_}
    
    # Ensure plugins directory exists for plugin discovery
    pluginsDir = CONFIG['PATHS']['plugins_dir']
    if not os.path.exists(pluginsDir):
        os.makedirs(pluginsDir)
        logging.info(f"Created plugins directory: {pluginsDir}")
        
    # Create history directory structure for event storage
    # This prevents runtime errors when trying to store events
    historyDir = CONFIG['PATHS']['history_dir']
    historySubdirs = ['Friend', 'Group', 'Others']
    for subdir in historySubdirs:
        fullPath = os.path.join(historyDir, subdir)
        if not os.path.exists(fullPath):
            os.makedirs(fullPath)
            logging.info(f"Created history directory: {fullPath}")
    
    # Validate plugins directory before attempting plugin discovery
    if not os.path.isdir(pluginsDir):
        logging.error(f"Plugins path is not a directory: {pluginsDir}")
        return
        
    # Discover plugin files: .py files excluding Python special files
    # Excludes __init__.py, __pycache__, etc. to avoid importing system files
    pluginFiles_ = [f for f in os.listdir(pluginsDir) if f.endswith('.py') and not f.startswith('__')]
    
    if not pluginFiles_:
        logging.warning(f"No plugin files found in {pluginsDir}")
        return
    
    logging.info(f"Found {len(pluginFiles_)} plugin files: {pluginFiles_}")
    
    # Process each plugin file individually
    # Failures in one plugin don't affect others
    for pluginFile in pluginFiles_:
        try:
            # Extract module name by removing .py extension
            moduleName = pluginFile[:-3]
            
            # Add plugins directory to Python path for import
            # This allows importing plugin modules by name
            import sys
            if pluginsDir not in sys.path:
                sys.path.insert(0, pluginsDir)
            
            # Dynamically import the plugin module
            pluginModule = importlib.import_module(moduleName)
            
            # Validate plugin structure: must have MANIFEST
            if not hasattr(pluginModule, 'MANIFEST'):
                logging.warning(f"Plugin {moduleName} has no MANIFEST, skipping")
                continue
                
            manifest = getattr(pluginModule, 'MANIFEST')
            if not isinstance(manifest, dict):
                logging.warning(f"Plugin {moduleName} MANIFEST is not a dict, skipping")
                continue
            
            # Process each handler declared in the plugin manifest
            # MANIFEST format: {"event_type": "function_name", ...}
            for eventType, functionName in manifest.items():
                # Validate event type against known OneBot 11 events
                if eventType not in EVENT_TYPES_:
                    logging.warning(f"Plugin {moduleName} declares invalid event type '{eventType}'. Valid types: {EVENT_TYPES_}")
                    continue
                    
                # Validate function exists in the plugin module
                if not hasattr(pluginModule, functionName):
                    logging.warning(f"Plugin {moduleName} declares function '{functionName}' but it doesn't exist")
                    continue
                    
                # Extract and validate the handler function
                handlerFunction = getattr(pluginModule, functionName)
                if not callable(handlerFunction):
                    logging.warning(f"Plugin {moduleName}.{functionName} is not callable")
                    continue
                
                # Register the handler function for the event type
                # Multiple plugins can handle the same event type
                PLUGIN_REGISTRY[eventType].append(handlerFunction)
                logging.info(f"Registered {moduleName}.{functionName} for event '{eventType}'")
                
        except Exception as e:
            # Log import/processing errors but continue with other plugins
            logging.error(f"Failed to load plugin {pluginFile}: {e}")
            continue
    
    # Log comprehensive statistics about plugin loading results
    totalHandlers = sum(len(handlerList_) for handlerList_ in PLUGIN_REGISTRY.values())
    logging.info(f"Plugin initialization complete. {totalHandlers} handlers registered for {len(PLUGIN_REGISTRY)} event types")
    
    # Log detailed breakdown of registered handlers per event type
    # Only show event types that actually have handlers to reduce log noise
    for eventType, handlerList_ in PLUGIN_REGISTRY.items():
        if handlerList_:  # Only log event types that have handlers
            logging.info(f"  {eventType}: {len(handlerList_)} handlers")

# ============================================================================
# EVENT PROCESSING FUNCTIONS
# ============================================================================

def EventTypeParser(rawEvent: Dict) -> str:
    """
    Parse OneBot 11 event structure to determine standardized event type.
    
    OneBot 11 uses a hierarchical event structure with post_type as the primary
    classifier and various sub-type fields for specific event identification.
    This function maps the complex OneBot structure to our simplified event types.
    
    Event Structure Analysis:
    - post_type: Primary event category (message, notice, request, meta_event)
    - message_type/notice_type/etc: Secondary classification
    - sub_type: Tertiary classification for some events
    
    Special Cases:
    - "notify" notice_type requires sub_type analysis for proper classification
    - "message_sent" is treated separately from regular "message" events
    - Unknown/malformed events return "UNEXPECTED" for proper error handling
    
    Args:
        rawEvent (Dict): Complete OneBot 11 event data from NapCat
                        Expected to contain at minimum 'post_type' field
    
    Returns:
        str: Standardized event type from EVENT_TYPES_ list, or "UNEXPECTED"
             for unrecognized event structures
    
    Example Input/Output:
        {"post_type": "message", "message_type": "private"} -> "MESSAGE_PRIVATE"
        {"post_type": "notice", "notice_type": "notify", "sub_type": "poke"} -> "NOTICE_POKE"
        {"post_type": "unknown"} -> "UNEXPECTED"
    """
    # Primary classification by post_type
    match rawEvent.get("post_type"):
        case "message":
            # Regular message events (incoming)
            match rawEvent.get("message_type"):
                case "private":
                    return "MESSAGE_PRIVATE"
                case "group":
                    return "MESSAGE_GROUP"
        
        case "message_sent":
            # Bot's own messages (outgoing) - separate from incoming for different handling
            match rawEvent.get("message_type"):
                case "private":
                    return "MESSAGE_SENT_PRIVATE"
                case "group":
                    return "MESSAGE_SENT_GROUP"
        
        case "notice":
            # System notifications and platform events
            match rawEvent.get("notice_type"):
                case "friend_add":
                    return "NOTICE_FRIEND_ADD"
                case "friend_recall":
                    return "NOTICE_FRIEND_RECALL"
                case "group_recall":
                    return "NOTICE_GROUP_RECALL"
                case "group_increase":
                    return "NOTICE_GROUP_INCREASE"
                case "group_decrease":
                    return "NOTICE_GROUP_DECREASE"
                case "group_admin":
                    return "NOTICE_GROUP_ADMIN"
                case "group_ban":
                    return "NOTICE_GROUP_BAN"
                case "group_upload":
                    return "NOTICE_GROUP_UPLOAD"
                case "group_card":
                    return "NOTICE_GROUP_CARD"
                case "essence":
                    return "NOTICE_ESSENCE"
                case "group_msg_emoji_like":
                    return "NOTICE_GROUP_MSG_EMOJI_LIKE"
                case "bot_offline":
                    return "NOTICE_BOT_OFFLINE"
                case "notify":
                    # Special case: notify events require sub_type analysis
                    # These are miscellaneous notifications that don't fit other categories
                    match rawEvent.get("sub_type"):
                        case "group_name":
                            return "NOTICE_GROUP_NAME"
                        case "title":
                            return "NOTICE_GROUP_TITLE"
                        case "poke":
                            return "NOTICE_POKE"
                        case "profile_like":
                            return "NOTICE_PROFILE_LIKE"
                        case "input_status":
                            return "NOTICE_INPUT_STATUS"
        
        case "request":
            # Events requiring user/admin approval
            match rawEvent.get("request_type"):
                case "friend":
                    return "REQUEST_FRIEND"
                case "group":
                    return "REQUEST_GROUP"
        
        case "meta_event":
            # Protocol-level events (heartbeats, lifecycle changes)
            match rawEvent.get("meta_event_type"):
                case "heartbeat":
                    return "META_HEARTBEAT"
                case "lifecycle":
                    return "META_LIFECYCLE"
    
    # Handle unrecognized event structures
    # Log detailed information for debugging malformed events
    logging.warning(f"Unrecognized event structure: post_type='{rawEvent.get('post_type')}', "
                   f"sub_type='{rawEvent.get('message_type', rawEvent.get('notice_type', rawEvent.get('request_type', rawEvent.get('meta_event_type'))))}', "
                   f"sub_sub_type='{rawEvent.get('sub_type')}'")
    return "UNEXPECTED"

def InbondEventDataParser(rawEvent: Dict) -> Optional[Dict]:
    """
    Convert complex OneBot 11 event data into simplified format for plugin consumption.
    
    OneBot 11 events contain extensive metadata and complex message structures.
    This parser extracts the essential information that most plugins need,
    particularly focusing on text content extraction from message arrays.
    
    Message Structure Handling:
    OneBot 11 messages are arrays of segments with different types:
    - text: Contains actual text content
    - image: Contains image data (ignored)
    - at: Contains @mentions (ignored)
    - etc: Various multimedia and special content (ignored)
    
    This parser specifically extracts text segments and concatenates them
    to provide a clean text representation of the message.
    
    Args:
        rawEvent (Dict): Complete OneBot 11 event from NapCat
    
    Returns:
        Optional[Dict]: Simplified event data for plugins, or None for unsupported events
        
        For MESSAGE_PRIVATE:
            {
                "user_id": int,      # Sender's QQ number
                "text_message": str  # Concatenated text content
            }
            
        For MESSAGE_GROUP:
            {
                "user_id": int,      # Sender's QQ number
                "group_id": int,     # Group's ID number
                "text_message": str  # Concatenated text content
            }
            
        For other events: None (not yet implemented)
    
    Example:
        Input: {
            "post_type": "message",
            "message_type": "private", 
            "user_id": 123456,
            "message": [
                {"type": "text", "data": {"text": "Hello "}},
                {"type": "image", "data": {...}},
                {"type": "text", "data": {"text": "world!"}}
            ]
        }
        Output: {"user_id": 123456, "text_message": "Hello world!"}
    """
    # Determine event type to decide processing method
    eventType = EventTypeParser(rawEvent)
    
    match eventType:
        case "MESSAGE_PRIVATE":
            # Extract text content from message segment array
            messageSegments = rawEvent.get("message", [])
            textParts = []
            
            # Process each message segment
            for segment in messageSegments:
                if segment.get("type") == "text":
                    # Extract text data from text segments
                    textData = segment.get("data", {})
                    textContent = textData.get("text", "")
                    textParts.append(textContent)
                # Ignore non-text segments (images, audio, etc.)
            
            # Concatenate all text parts to form complete message
            fullText = "".join(textParts)
            
            return {
                "user_id": rawEvent.get("user_id"),
                "text_message": fullText
            }
            
        case "MESSAGE_GROUP":
            # Same text extraction logic as private messages
            messageSegments = rawEvent.get("message", [])
            textParts = []
            
            for segment in messageSegments:
                if segment.get("type") == "text":
                    textData = segment.get("data", {})
                    textContent = textData.get("text", "")
                    textParts.append(textContent)
            
            fullText = "".join(textParts)
            
            return {
                "user_id": rawEvent.get("user_id"),
                "group_id": rawEvent.get("group_id"),
                "text_message": fullText
            }
            
        # Placeholder cases for future implementation
        # All other event types return None, indicating no simplified data available
        case "MESSAGE_SENT_PRIVATE":
            return None
        case "MESSAGE_SENT_GROUP":
            return None
        case "NOTICE_FRIEND_ADD":
            return None
        case "NOTICE_FRIEND_RECALL":
            return None
        case "NOTICE_GROUP_RECALL":
            return None
        case "NOTICE_GROUP_INCREASE":
            return None
        case "NOTICE_GROUP_DECREASE":
            return None
        case "NOTICE_GROUP_ADMIN":
            return None
        case "NOTICE_GROUP_BAN":
            return None
        case "NOTICE_GROUP_UPLOAD":
            return None
        case "NOTICE_GROUP_CARD":
            return None
        case "NOTICE_GROUP_NAME":
            return None
        case "NOTICE_GROUP_TITLE":
            return None
        case "NOTICE_POKE":
            return None
        case "NOTICE_PROFILE_LIKE":
            return None
        case "NOTICE_INPUT_STATUS":
            return None
        case "NOTICE_ESSENCE":
            return None
        case "NOTICE_GROUP_MSG_EMOJI_LIKE":
            return None
        case "NOTICE_BOT_OFFLINE":
            return None
        case "REQUEST_FRIEND":
            return None
        case "REQUEST_GROUP":
            return None
        case "META_HEARTBEAT":
            return None
        case "META_LIFECYCLE":
            return None
        case "UNEXPECTED":
            return None
        case _:
            return None

def PluginCaller(handler, simpleEvent: Optional[Dict], rawEvent: Dict):
    """
    Safely execute plugin handler functions with comprehensive error handling.
    
    Plugin functions are user-provided code that may contain bugs, infinite loops,
    or other issues. This wrapper ensures that plugin failures don't crash the
    entire system by catching and logging all exceptions.
    
    Plugin Function Contract:
    All plugin functions must accept exactly two arguments:
    1. simpleEvent: Parsed/simplified event data (may be None for unsupported events)
    2. rawEvent: Complete OneBot 11 event data (always provided)
    
    Args:
        handler: Plugin function to execute (callable)
        simpleEvent (Optional[Dict]): Simplified event data from InbondEventDataParser
        rawEvent (Dict): Complete OneBot 11 event data
    
    Returns:
        Any: Plugin function return value, or None if execution failed
        
        Valid plugin responses can be:
        - str: Simple text reply
        - dict: Structured API call with 'action' and 'data' keys
        - list: Multiple actions to execute
        - None: No response (plugin chose not to respond)
    
    Error Handling:
    - All exceptions are caught and logged with function name
    - Failed calls return None, allowing other plugins to still execute
    - No exceptions propagate to calling code
    """
    try:
        # Execute plugin function with both simple and raw event data
        return handler(simpleEvent, rawEvent)
    except Exception as e:
        # Log plugin errors with function name for debugging
        # Include full exception details to help plugin developers
        logging.error(f"Plugin {handler.__name__} failed: {e}")
        return None

def NapCatSender(actionEndpoint: str, requestBody: Dict) -> None:
    """
    Send HTTP requests to NapCat API with comprehensive retry logic and error handling.
    
    NapCat provides an HTTP API for sending messages and performing actions.
    This function handles the complexities of HTTP communication including
    timeouts, retries, status validation, and error reporting.
    
    OneBot 11 API Response Format:
    Successful responses have this structure:
    {
        "status": "ok" | "async",    # "ok" = completed, "async" = queued
        "retcode": 0,                # 0 = success, other = error code
        "data": {...}                # Response data (varies by endpoint)
    }
    
    Error Handling Strategy:
    - 4xx errors: Client errors, don't retry (bad request format)
    - 5xx errors: Server errors, retry with exponential backoff
    - Timeouts: Network issues, retry up to configured limit
    - Invalid responses: JSON parsing errors, treated as failures
    
    Args:
        actionEndpoint (str): API endpoint path (e.g., "send_private_msg")
        requestBody (Dict): JSON payload for the API request
    
    Returns:
        None: Function handles all responses internally
    
    Side Effects:
        - Makes HTTP POST request to NapCat API
        - Logs warnings/errors for debugging
        - On failure, attempts to query /get_status for system diagnostics
    
    Example Usage:
        NapCatSender("send_private_msg", {"user_id": 123456, "message": [...]})
    """
    # Construct full API URL from base configuration
    baseUrl = CONFIG['NAPCAT_SERVER']['api_url']
    fullUrl = f"{baseUrl}/{actionEndpoint}"
    
    # Retry loop with configurable attempts
    for attempt in range(CONFIG['HTTP']['max_retries']):
        try:
            # Make HTTP POST request with JSON payload
            response = requests.post(
                fullUrl,
                json=requestBody,
                timeout=CONFIG['HTTP']['timeout_seconds']
            )
            
            # Handle client errors (4xx) - don't retry these
            # These indicate malformed requests that won't succeed on retry
            if 400 <= response.status_code < 500:
                logging.warning(f"Client error {response.status_code} for {actionEndpoint}")
                return
            
            # Handle successful responses (200 OK)
            if response.status_code == 200:
                try:
                    # Parse JSON response and validate OneBot 11 status
                    responseData = response.json()
                    status = responseData.get('status', '').lower()
                    if status in ['ok', 'async']:
                        return  # Success - exit function
                    else:
                        # Got 200 but API reported failure
                        logging.error(f"API returned status '{status}' for {actionEndpoint}")
                        break  # Exit retry loop, go to status check
                except (json.JSONDecodeError, KeyError) as e:
                    # Invalid JSON response from API
                    logging.error(f"Invalid JSON response from {actionEndpoint}: {e}")
                    break  # Exit retry loop, go to status check
            
            # Handle server errors (5xx) and other status codes - retry these
            logging.warning(f"HTTP {response.status_code} from {actionEndpoint}, attempt {attempt + 1}/{CONFIG['HTTP']['max_retries']}")
            
        except requests.exceptions.Timeout:
            # Network timeout - retry with warning
            logging.warning(f"Timeout for {actionEndpoint}, attempt {attempt + 1}/{CONFIG['HTTP']['max_retries']}")
            # Continue to next iteration (retry)
            
        except Exception as e:
            # Other network/request errors - don't retry
            logging.error(f"Request error for {actionEndpoint}: {e}")
            break  # Don't retry on other request errors
    
    # If we reach here, all retries failed or API returned error status
    # Attempt to get system status for diagnostic information
    try:
        statusUrl = f"{baseUrl}/get_status"
        statusResponse = requests.get(statusUrl, timeout=CONFIG['HTTP']['status_check_timeout'])
        if statusResponse.status_code == 200:
            statusData = statusResponse.json()
            logging.error(f"Failed to send {actionEndpoint}. Bot status: {statusData}")
        else:
            logging.error(f"Failed to send {actionEndpoint}. Could not get bot status (HTTP {statusResponse.status_code})")
    except Exception as e:
        logging.error(f"Failed to send {actionEndpoint}. Could not get bot status: {e}")

def OutbondEventDataParser(pluginResponse: Any, rawEvent: Dict) -> None:
    """
    Convert plugin responses into proper OneBot 11 API calls to NapCat.
    
    Plugins can return responses in multiple formats for flexibility:
    1. String: Simple text reply to the received message
    2. Dict: Structured API call with specific action and parameters
    3. List: Multiple actions to perform in sequence
    
    This function validates plugin responses, converts them to proper OneBot 11
    API format, and dispatches them to NapCat via HTTP.
    
    Response Format Handling:
    
    String Response:
    - Treated as simple text reply to the current message
    - Only works for message events (private/group)
    - Automatically wrapped in OneBot 11 message segment format
    
    Dict Response:
    - Must contain 'action' and 'data' keys
    - 'action': OneBot 11 API endpoint name
    - 'data': Parameters for the API call
    - Only send_private_msg and send_group_msg are fully implemented
    
    List Response:
    - Array of dict responses (as described above)
    - Processed sequentially, each as a separate API call
    
    Args:
        pluginResponse (Any): Plugin function return value
        rawEvent (Dict): Original OneBot 11 event (for context in string responses)
    
    Returns:
        None: Function dispatches API calls internally
    
    Side Effects:
        - Calls NapCatSender() for each valid response
        - Logs warnings for invalid response formats
        - Logs errors for missing required fields
    
    Example Responses:
        String: "Hello back!"
        Dict: {"action": "send_private_msg", "data": {"user_id": 123, "message": [...]}}
        List: [{"action": "send_private_msg", ...}, {"action": "send_group_msg", ...}]
    """
    # Handle different plugin response types
    if isinstance(pluginResponse, str):
        # String responses: Simple text reply to current message
        
        # Validate that this is a message event (can be replied to)
        if rawEvent.get("post_type") != "message":
            logging.warning("Plugin returned string response for non-message event")
            return
            
        messageType = rawEvent.get("message_type")
        if messageType == "private":
            # Reply to private message
            requestBody = {
                "user_id": rawEvent.get("user_id"),
                # Convert string to OneBot 11 message segment format
                "message": [{"type": "text", "data": {"text": f"{pluginResponse}\n"}}]
            }
            NapCatSender("send_private_msg", requestBody)
            
        elif messageType == "group":
            # Reply to group message
            requestBody = {
                "group_id": rawEvent.get("group_id"),
                # Convert string to OneBot 11 message segment format
                "message": [{"type": "text", "data": {"text": f"{pluginResponse}\n"}}]
            }
            NapCatSender("send_group_msg", requestBody)
            
        else:
            logging.warning(f"Unknown message_type '{messageType}' for string response")
            
    elif isinstance(pluginResponse, dict):
        # Dict responses: Structured API calls
        
        # Validate required keys in response structure
        if "action" not in pluginResponse or "data" not in pluginResponse:
            logging.warning("Plugin dict response missing 'action' or 'data' keys")
            return
            
        action = pluginResponse["action"]
        data = pluginResponse["data"]
        
        # Validate data field type
        if not isinstance(data, dict):
            logging.warning("Plugin response 'data' field is not a dict")
            return
            
        # Initialize variables for request processing
        requestBody = {}
        actionEndpoint = ""
        
        # Map plugin actions to OneBot 11 API endpoints
        # This match statement serves as an API catalog for future implementation
        match action:
            case "send_private_msg":
                # Send private message - fully implemented
                requestBody = {
                    "user_id": None,    # Required: recipient QQ number
                    "message": None     # Required: message content (segments)
                }
                actionEndpoint = "send_private_msg"
                
            case "send_group_msg":
                # Send group message - fully implemented
                requestBody = {
                    "group_id": None,   # Required: target group ID
                    "message": None     # Required: message content (segments)
                }
                actionEndpoint = "send_group_msg"
                
            # Future implementation placeholders - OneBot 11 API catalog
            # These actions are recognized but not yet implemented
            case "send_msg":
                return
            case "delete_msg":
                return
            case "get_msg":
                return
            case "get_forward_msg":
                return
            case "send_like":
                return
            case "set_group_kick":
                return
            case "set_group_ban":
                return
            case "set_group_anonymous_ban":
                return
            case "set_group_whole_ban":
                return
            case "set_group_admin":
                return
            case "set_group_anonymous":
                return
            case "set_group_card":
                return
            case "set_group_name":
                return
            case "set_group_leave":
                return
            case "set_group_special_title":
                return
            case "set_friend_add_request":
                return
            case "set_group_add_request":
                return
            case "get_login_info":
                return
            case "get_stranger_info":
                return
            case "get_friend_list":
                return
            case "get_group_info":
                return
            case "get_group_list":
                return
            case "get_group_member_info":
                return
            case "get_group_member_list":
                return
            case "get_group_honor_info":
                return
            case "get_cookies":
                return
            case "get_csrf_token":
                return
            case "get_credentials":
                return
            case "get_record":
                return
            case "get_image":
                return
            case "can_send_image":
                return
            case "can_send_record":
                return
            case "get_status":
                return
            case "get_version_info":
                return
            case "set_restart":
                return
            case "clean_cache":
                return
            case _:
                # Unknown action - log and skip
                logging.warning(f"Unknown action '{action}' in plugin response")
                return
        
        # Process implemented actions (send_private_msg, send_group_msg)
        # Update request body with plugin-provided data
        for key, value in data.items():
            if key in requestBody:
                # Valid field - use plugin value
                requestBody[key] = value
            else:
                # Extra field - likely plugin bug
                logging.warning(f"Plugin provided extra field '{key}' for action '{action}'")
        
        # Validate that all required fields are provided
        # Required fields are initialized with None and must be replaced
        missingFields = [key for key, value in requestBody.items() if value is None]
        if missingFields:
            logging.warning(f"Missing required fields for {action}: {missingFields}")
            return
            
        # Dispatch validated API call to NapCat
        NapCatSender(actionEndpoint, requestBody)
            
    elif isinstance(pluginResponse, list):
        # List responses: Multiple actions to perform
        # Process each item recursively as individual response
        for item in pluginResponse:
            OutbondEventDataParser(item, rawEvent)
            
    else:
        # Invalid response type
        logging.warning(f"Invalid plugin response type: {type(pluginResponse)}")
        return

def MainDispatcher(rawEvent: Dict) -> None:
    """
    Main event processing pipeline - orchestrates the entire plugin system workflow.
    
    This is the central coordinator that receives OneBot 11 events from NapCat
    and manages the complete processing pipeline from event parsing to response
    generation. It implements the core plugin architecture pattern where multiple
    plugins can independently process the same event.
    
    Processing Pipeline:
    1. Event Type Classification: Determine what kind of event this is
    2. Early Filtering: Skip malformed/unsupported events
    3. Data Simplification: Convert complex OneBot data to plugin-friendly format
    4. History Storage: Asynchronously store event for future reference
    5. Plugin Execution: Call all registered handlers for this event type
    6. Response Processing: Convert plugin responses to API calls
    
    Concurrent Execution Model:
    - History storage runs in background thread (non-blocking)
    - Plugin handlers execute sequentially in main thread
    - Each plugin response is processed immediately
    - Plugin failures don't affect other plugins
    
    Args:
        rawEvent (Dict): Complete OneBot 11 event data from NapCat webhook
                        Expected to contain post_type and relevant sub-fields
    
    Returns:
        None: Function coordinates all processing internally
    
    Side Effects:
        - Triggers async history storage
        - Executes multiple plugin functions
        - May generate multiple API calls to NapCat
        - Logs processing steps and errors
    
    Error Handling:
        - Malformed events are logged and skipped
        - Plugin failures are isolated and logged
        - History storage failures don't block response processing
    """
    # Step 1: Classify the event type using OneBot 11 structure
    eventType = EventTypeParser(rawEvent)
    
    # Step 2: Early exit for malformed/unrecognized events
    # This prevents wasted processing on invalid data
    if eventType == "UNEXPECTED":
        return
        
    # Step 3: Convert complex event data to simplified format for plugins
    # This may return None for event types not yet supported
    simpleEvent = InbondEventDataParser(rawEvent)
    
    # Step 4: Store event to history files for future reference
    # Uses background thread to avoid blocking real-time response processing
    # History failures are logged but don't affect message processing
    HISTORIAN_EXECUTOR.submit(asyncio.run, Historian(rawEvent))
    
    # Step 5: Execute all registered plugin handlers for this event type
    # Multiple plugins can handle the same event independently
    handlerList_ = PLUGIN_REGISTRY.get(eventType, [])
    for handler in handlerList_:
        # Execute plugin function with error isolation
        pluginResponse = PluginCaller(handler, simpleEvent, rawEvent)
        
        # Step 6: Process non-null responses into API calls
        # Plugins returning None are considered "no response"
        if pluginResponse is not None:
            OutbondEventDataParser(pluginResponse, rawEvent)
    
    return

# ============================================================================
# MESSAGE HISTORY FUNCTIONS
# ============================================================================

async def Historian(rawEvent: Dict) -> None:
    """
    Asynchronously store events to organized JSON files with retry logic.
    
    The Historian maintains a comprehensive log of all events for context-aware
    bot responses. Events are organized by type and participant for efficient
    retrieval by plugins needing conversation history.
    
    File Organization Strategy:
    - Friend/{user_id}.json: Private conversations and friend-specific events
    - Group/{group_id}.json: Group conversations and group-specific events  
    - Others/{event_type}.json: Meta events, requests, and misc events
    
    Event Classification Rules:
    1. Has user_id but no group_id → Friend file
    2. Has group_id → Group file (regardless of user_id presence)
    3. Special case NOTICE_POKE: group_id present → Group, else → Friend
    4. Everything else → Others file by event type
    5. NOTICE_INPUT_STATUS → Not stored (too noisy)
    
    Storage Format:
    Each file contains a JSON array of complete OneBot 11 events:
    [event1, event2, event3, ...]
    
    Events are appended chronologically for natural conversation flow.
    
    Retry Logic:
    File operations can fail due to disk issues, permissions, or concurrent access.
    The function retries up to 3 times with 3-second delays to handle transient
    issues like temporary disk full conditions or file locking.
    
    Args:
        rawEvent (Dict): Complete OneBot 11 event to store
    
    Returns:
        None: Function handles all storage internally
    
    Side Effects:
        - Creates/modifies JSON files in MessageHistory directory
        - Logs warnings for retry attempts
        - Logs errors for final failures
    
    Async Behavior:
        - Runs in background thread via ThreadPoolExecutor
        - Uses asyncio.sleep() for non-blocking retry delays
        - Does not block main event processing pipeline
    """
    eventType = EventTypeParser(rawEvent)
    
    # Skip noisy input status events to keep logs manageable
    if eventType == "NOTICE_INPUT_STATUS":
        return
    
    # Determine appropriate storage file based on event type and content
    historyDir = CONFIG['PATHS']['history_dir']
    filePath = None
    
    # Friend-specific events: private conversations and friend interactions
    # These events have user_id but no group_id (or group_id is None)
    if eventType in ["MESSAGE_PRIVATE", "NOTICE_FRIEND_RECALL", "NOTICE_FRIEND_ADD", "NOTICE_PROFILE_LIKE"]:
        userId = rawEvent.get("user_id")
        if userId:
            filePath = os.path.join(historyDir, "Friend", f"{userId}.json")
    
    # Group-specific events: group conversations and group management
    # These events always have group_id present
    elif eventType in ["MESSAGE_GROUP", "NOTICE_GROUP_RECALL", "NOTICE_GROUP_INCREASE", 
                      "NOTICE_GROUP_DECREASE", "NOTICE_GROUP_ADMIN", "NOTICE_GROUP_BAN",
                      "NOTICE_GROUP_UPLOAD", "NOTICE_GROUP_CARD", "NOTICE_ESSENCE",
                      "NOTICE_GROUP_MSG_EMOJI_LIKE", "NOTICE_GROUP_NAME", "NOTICE_GROUP_TITLE"]:
        groupId = rawEvent.get("group_id")
        if groupId:
            filePath = os.path.join(historyDir, "Group", f"{groupId}.json")
    
    # Special case: NOTICE_POKE can be either private or group context
    elif eventType == "NOTICE_POKE":
        groupId = rawEvent.get("group_id")
        userId = rawEvent.get("user_id")
        if groupId:
            # Poke happened in group chat - store with group events
            filePath = os.path.join(historyDir, "Group", f"{groupId}.json")
        elif userId:
            # Private poke - store with friend events
            filePath = os.path.join(historyDir, "Friend", f"{userId}.json")
    
    # Fallback: All other events go to Others directory by event type
    if not filePath:
        filePath = os.path.join(historyDir, "Others", f"{eventType}.json")
    
    # File operation with retry logic for reliability
    maxRetries = 3
    for attempt in range(maxRetries):
        try:
            # Read existing events from file (if it exists)
            if os.path.exists(filePath):
                with open(filePath, 'r', encoding='utf-8') as f:
                    events = json.load(f)
            else:
                # Create new event list for new files
                events = []
            
            # Append new event to the chronological list
            events.append(rawEvent)
            
            # Write updated events back to file
            # Use ensure_ascii=False to support Unicode content in messages
            # Use indent=2 for human-readable formatting
            with open(filePath, 'w', encoding='utf-8') as f:
                json.dump(events, f, ensure_ascii=False, indent=2)
            
            # Success - exit retry loop
            return
            
        except Exception as e:
            # Log retry attempts for debugging
            logging.warning(f"Historian attempt {attempt + 1}/{maxRetries} failed for {filePath}: {e}")
            if attempt < maxRetries - 1:
                # Wait before retry to handle transient issues
                await asyncio.sleep(3)
            else:
                # All retries exhausted - log final failure
                logging.error(f"Historian failed after {maxRetries} attempts for {filePath}: {e}")
                return

# ============================================================================
# PLUGIN HELPER FUNCTIONS
# ============================================================================

def Librarian(eventIdentifier: Dict, eventCount: int = 50) -> List[Dict]:
    """
    Retrieve chat history for plugins requiring conversational context.
    
    Many intelligent chatbots need access to previous messages to provide
    context-aware responses. The Librarian provides a simple interface for
    plugins to access stored conversation history organized by participant.
    
    EventIdentifier Format:
    The eventIdentifier dict specifies what history to retrieve:
    
    Private Chat History:
        {"type": "private", "user_id": 123456}
        
    Group Chat History:
        {"type": "group", "group_id": 789012}
        
    Other Event History:
        {"type": "other", "event_type": "META_HEARTBEAT"}
    
    Return Format:
    Returns a list of complete OneBot 11 events in chronological order
    (oldest first, newest last). Each event is the complete original event
    as stored by the Historian, including all metadata and fields.
    
    Performance Considerations:
    - Only loads requested files (no global scanning)
    - Returns most recent N events (tail of file)
    - File size should be reasonable for real-time access
    - Consider implementing pagination for very large histories
    
    Args:
        eventIdentifier (Dict): Specifies which history to retrieve
                               Must contain 'type' field and relevant ID field
        eventCount (int): Maximum number of recent events to return
                         Defaults to 50 for reasonable memory usage
    
    Returns:
        List[Dict]: List of OneBot 11 events in chronological order
                   Empty list if no history exists or on error
                   
    Error Handling:
        - Missing files return empty list (not an error)
        - Invalid identifiers return empty list with warning
        - File read errors return empty list with error log
        - Invalid eventCount values are handled gracefully
    
    Example Usage:
        # Get last 10 messages with user 123456
        history = Librarian({"type": "private", "user_id": 123456}, 10)
        
        # Get recent group events
        history = Librarian({"type": "group", "group_id": 789012}, 20)
    """
    historyDir = CONFIG['PATHS']['history_dir']
    
    try:
        # Determine target file path from event identifier
        identifierType = eventIdentifier.get("type")
        
        match identifierType:
            case "private":
                # Private conversation history
                userId = eventIdentifier.get("user_id")
                if not userId:
                    logging.warning("Librarian: private type missing user_id")
                    return []
                filePath = os.path.join(historyDir, "Friend", f"{userId}.json")
                
            case "group":
                # Group conversation history
                groupId = eventIdentifier.get("group_id")
                if not groupId:
                    logging.warning("Librarian: group type missing group_id")
                    return []
                filePath = os.path.join(historyDir, "Group", f"{groupId}.json")
                
            case "other":
                # Other event type history
                eventType = eventIdentifier.get("event_type")
                if not eventType:
                    logging.warning("Librarian: other type missing event_type")
                    return []
                filePath = os.path.join(historyDir, "Others", f"{eventType}.json")
                
            case _:
                # Unknown identifier type
                logging.warning(f"Librarian: unknown identifier type '{identifierType}'")
                return []
        
        # Check if history file exists
        if not os.path.exists(filePath):
            return []  # No history file means no events - not an error
            
        # Load and parse history file
        with open(filePath, 'r', encoding='utf-8') as f:
            allEvents = json.load(f)
            
        # Return requested number of most recent events
        if eventCount <= 0:
            return []  # Invalid count
        elif eventCount >= len(allEvents):
            return allEvents  # Return all events if count exceeds available
        else:
            return allEvents[-eventCount:]  # Return last N events
            
    except Exception as e:
        # Log file read errors but don't crash
        logging.error(f"Librarian failed to read history: {e}")
        return []

# ============================================================================
# FLASK APPLICATION
# ============================================================================

# Create Flask application instance for receiving NapCat webhooks
NAPCAT_LISTENER = Flask(__name__)

@NAPCAT_LISTENER.route('/', methods=['POST'])
def NapCatListener() -> str:
    """
    Flask webhook endpoint for receiving OneBot 11 events from NapCat.
    
    NapCat sends all events (messages, notices, requests, meta events) to this
    endpoint via HTTP POST. The request body contains the complete OneBot 11
    event data in JSON format.
    
    This function serves as the entry point into the plugin system, immediately
    passing received events to the MainDispatcher for processing.
    
    Expected Request Format:
    - Method: POST
    - Content-Type: application/json
    - Body: OneBot 11 event JSON
    - Headers: x-self-id (bot QQ number)
    
    Processing Flow:
    1. Extract JSON event data from request body
    2. Pass to MainDispatcher for plugin processing
    3. Return success response to NapCat
    
    Returns:
        str: Simple "OK" response to acknowledge receipt
             NapCat doesn't process the response content
    
    Error Handling:
        - Malformed JSON will cause Flask to return 400 automatically
        - Plugin processing errors are handled in MainDispatcher
        - Always returns 200 OK to prevent NapCat retry loops
    """
    # Extract OneBot 11 event data from POST request
    rawEvent = request.get_json()
    
    # Dispatch to main processing pipeline
    MainDispatcher(rawEvent)
    
    # Return simple acknowledgment to NapCat
    return 'OK'

# ============================================================================
# MAIN EXECUTION
# ============================================================================

if __name__ == '__main__':
    """
    Application entry point - initialize system and start HTTP server.
    
    Startup sequence:
    1. Initialize plugin system (load plugins, create directories)
    2. Start Flask HTTP server to receive NapCat webhooks
    3. Run indefinitely until interrupted (Ctrl+C)
    
    The server binds to all interfaces (0.0.0.0) to accept connections from
    NapCat running on the same machine or remotely. Configure firewall rules
    appropriately for your deployment environment.
    """
    # Initialize plugin system and create required directories
    Initializer()
    
    # Start HTTP server for receiving NapCat events
    # This blocks until the server is stopped (Ctrl+C)
    NAPCAT_LISTENER.run(
        host=CONFIG['NAPCAT_LISTEN']['host'], 
        port=CONFIG['NAPCAT_LISTEN']['port']
    )