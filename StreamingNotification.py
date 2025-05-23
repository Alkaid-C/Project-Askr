"""
Bilibili Live Stream Monitor with NapCat Notification System

OVERVIEW:
This script creates an automated notification system for Bilibili live streams. It:
1. Monitors a specific Bilibili room using BililiveRecorder (BREC)  
2. Receives webhook notifications when stream starts/ends
3. Sends customizable notifications to QQ groups/friends via NapCat

ARCHITECTURE:
┌─────────────────┐    Webhook    ┌──────────────────┐    HTTP     ┌─────────────────┐
│ BililiveRecorder│─────────────→ │ This Script      │────────────→│ NapCat (QQ Bot) │
│ (BREC)          │    POST       │ (Flask Server)   │   POST      │                 │
└─────────────────┘               └──────────────────┘             └─────────────────┘

EXTERNAL DEPENDENCIES:
- BililiveRecorder: https://rec.danmuji.org/ (Bilibili recording software)
- NapCat: https://github.com/NapNeko/NapCatQQ (QQ bot framework)
- Waitress: Production WSGI server for Flask

API REFERENCES:
- BREC Webhook API: https://rec.danmuji.org/reference/webhook/
- NapCat Send Message API: Uses /send_group_msg and /send_private_msg endpoints

CONFIGURATION:
All subscribers and message templates are stored in 'napcat_config.json'
See the companion JSON file for configuration format examples.

NAMING CONVENTIONS:
- Global variables: ALL_CAPS with underscores (e.g., ROOM_ID, BREC_CLI_PATH)
- Local variables: camelCase (e.g., groupId, friendPayload)
- Functions: PascalCase ending with agent/person words (e.g., NapcatRequestConstructor, MessageSender)
- Iterable variables: End with underscore if expected to be iterated (e.g., GROUP_IDS_, FRIEND_IDS_)
- Non-iterable collections: No trailing underscore (e.g., groupPayload, requestsList)
"""

import subprocess
import threading
import os
import signal
import time
import random
import json
from flask import Flask, request
import requests
from waitress import serve
from typing import List, Tuple, Dict, Any

# =============================================================================
# BREC (BililiveRecorder) CONFIGURATION
# =============================================================================
# BililiveRecorder is a tool for recording Bilibili live streams
# Download: https://github.com/BililiveRecorder/BililiveRecorder
BREC_CLI_PATH = "XXXXXXX"

# Target Bilibili room to monitor
ROOM_ID = 1921712061

# Where BREC will save recorded video files
BREC_OUTPUT_PATH = "XXXXXXXX"

# File naming template using BREC's template system
# See: https://rec.danmuji.org/docs/file-naming/
BREC_FILENAME_FORMAT = f"{{{{roomId}}}}/{{{{ \"now\" | time_zone: \"Asia/Shanghai\" | format_date: \"yyyyMMdd-HHmmss\" }}}}.flv"

# BREC Web UI settings (for remote management)
BREC_BIND_ADDRESS = "XXXXXXXX"
BREC_HTTP_USER = "XXXXXXXX"        
BREC_HTTP_PASS = "XXXXXXXX"      

# What types of danmaku (comments) to record along with the stream
BREC_DANMAKU_TYPES = "Danmaku,Gift,SuperChat"

# =============================================================================
# WEBHOOK SERVER CONFIGURATION  
# =============================================================================
# This script runs a Flask server to receive webhook notifications from BREC
# When stream events occur, BREC sends HTTP POST requests to this server

WEBHOOK_SERVER_HOST = "XXXXXXXX"
WEBHOOK_SERVER_PORT = XXXXXXXX    
WEBHOOK_ENDPOINT_PATH = "/webhook"  

# =============================================================================
# NAPCAT CONFIGURATION
# =============================================================================
# NapCat is a QQ bot framework that sends notifications to QQ users
# Project: https://github.com/NapNeko/NapCatQQ
# API Documentation: Check NapCat docs for /send_group_msg and /send_private_msg

NAPCAT_BASE_URL = "XXXXXXXX"
NAPCAT_GROUP_API_PATH = "/send_group_msg"      # Endpoint for sending to QQ groups
NAPCAT_FRIEND_API_PATH = "/send_private_msg"   # Endpoint for sending to individual users
NAPCAT_CONFIG_FILE = "napcat_config.json"     # External config file for subscribers & messages
NAPCAT_RETRY_ATTEMPTS = 3                     # How many times to retry failed requests
NAPCAT_RETRY_DELAY = 5                        # Seconds to wait between retries

# =============================================================================
# BREC STARTUP ARGUMENTS
# =============================================================================
# Command line arguments passed to BililiveRecorder
# Documentation: https://rec.danmuji.org/docs/basic/index.html

BREC_ARGS = [
    "portable",                    # Use portable mode (doesn't write to system dirs)
    BREC_OUTPUT_PATH,             # Output directory for recordings
    str(ROOM_ID),                 # Room ID to monitor
    "--bind", BREC_BIND_ADDRESS,  # Web UI binding address
    "--http-basic-user", BREC_HTTP_USER,    # Web UI username
    "--http-basic-pass", BREC_HTTP_PASS,    # Web UI password  
    "-f", BREC_FILENAME_FORMAT,   # File naming template
    "--webhook-url", f"http://{WEBHOOK_SERVER_HOST}:{WEBHOOK_SERVER_PORT}{WEBHOOK_ENDPOINT_PATH}",  # Where to send webhooks
    "--danmaku", BREC_DANMAKU_TYPES  # Types of comments to record
]

# =============================================================================
# GLOBAL VARIABLES
# =============================================================================
BREC_PROCESS = None  # Stores the BREC subprocess object for lifecycle management
BREC_HOOK = Flask(__name__)  # Flask application instance for handling webhooks

# =============================================================================
# CONFIGURATION MANAGEMENT
# =============================================================================

def NapcatConfigLoader(eventType: str) -> Tuple[List[int], List[int], List[List[Dict[str, Any]]], List[List[Dict[str, Any]]]]:
    """
    Loads NapCat configuration from external JSON file and extracts event-specific data.
    
    This function reads the configuration file and parses it to extract:
    - Which QQ groups/friends should receive notifications for this event type
    - What message templates are available for this event type
    
    Args:
        eventType: The type of event (e.g., "STREAMING_START", "STREAMING_ENDED")
    
    Returns:
        Tuple containing (groupSubscribers, friendSubscribers, groupTemplates, privateTemplates)
        - groupSubscribers: List of QQ group IDs that want this event type
        - friendSubscribers: List of QQ user IDs that want this event type  
        - groupTemplates: List of message templates for groups
        - privateTemplates: List of message templates for private messages
    
    Configuration File Format:
        See napcat_config.json for the expected JSON structure
    """
    try:
        with open(NAPCAT_CONFIG_FILE, 'r', encoding='utf-8') as configFile:
            config = json.load(configFile)
    except Exception as exception:
        print(f"Error: Failed to load configuration file '{NAPCAT_CONFIG_FILE}': {exception}")
        print("Please ensure the configuration file exists and contains valid JSON.")
        exit(1)
    
    # Extract data using chained .get() calls with safe defaults
    # This approach safely navigates the nested dictionary structure without throwing KeyError
    groupSubscribers = config.get('subscribers', {}).get(eventType, {}).get('groups', [])
    friendSubscribers = config.get('subscribers', {}).get(eventType, {}).get('friends', [])
    groupTemplates = config.get('messages', {}).get(eventType, {}).get('group_templates', [])
    privateTemplates = config.get('messages', {}).get(eventType, {}).get('private_templates', [])
    
    return groupSubscribers, friendSubscribers, groupTemplates, privateTemplates

# =============================================================================
# REQUEST CONSTRUCTION
# =============================================================================

def NapcatRequestConstructor(eventType: str) -> List[Tuple[str, Dict[str, Any]]]:
    """
    Constructs HTTP requests to send to NapCat for all subscribed groups and friends.
    
    This function:
    1. Loads configuration for the specific event type
    2. Validates that subscribers have corresponding message templates
    3. Randomly selects message templates for variety
    4. Builds HTTP request objects for each subscriber
    
    Args:
        eventType: Type of stream event (e.g., "STREAMING_START", "STREAMING_ENDED")
    
    Returns:
        List of tuples, each containing (url, payload) for an HTTP request
        Empty list if configuration is invalid or no subscribers exist
        
    NapCat API Details:
        - Groups use /send_group_msg endpoint with "group_id" parameter
        - Friends use /send_private_msg endpoint with "user_id" parameter
        - Message format follows NapCat's message structure specifications
    """
    requestsList = []
    
    # Load configuration from JSON file and extract event-specific data
    groupSubscribers, friendSubscribers, groupTemplates, privateTemplates = NapcatConfigLoader(eventType)
    
    # Validate configuration to prevent sending empty/broken messages
    # This catches common configuration mistakes like having subscribers but no message templates
    if ((not groupTemplates and groupSubscribers) or 
        (not privateTemplates and friendSubscribers) or 
        (not groupSubscribers and not friendSubscribers)):
        print(f"Warning: Configuration for event type '{eventType}' is not correct")
        return requestsList
    
    # Randomly select message templates for this event to add variety to notifications
    # This prevents users from seeing the exact same message every time
    selectedGroupMessage = None
    selectedPrivateMessage = None
    
    if groupTemplates and groupSubscribers:
        groupRandomIndex = random.randint(0, len(groupTemplates) - 1)
        selectedGroupMessage = groupTemplates[groupRandomIndex]
    
    if privateTemplates and friendSubscribers:
        privateRandomIndex = random.randint(0, len(privateTemplates) - 1)
        selectedPrivateMessage = privateTemplates[privateRandomIndex]
    
    # Generate HTTP requests for each subscribed QQ group
    # Uses NapCat's /send_group_msg API endpoint
    for groupId in groupSubscribers:
        groupUrl = f"{NAPCAT_BASE_URL}{NAPCAT_GROUP_API_PATH}"
        groupPayload = {
            "group_id": groupId,           # QQ group ID to send message to
            "message": selectedGroupMessage # Pre-formatted message structure
        }
        requestsList.append((groupUrl, groupPayload))
    
    # Generate HTTP requests for each subscribed QQ friend
    # Uses NapCat's /send_private_msg API endpoint  
    for friendId in friendSubscribers:
        friendUrl = f"{NAPCAT_BASE_URL}{NAPCAT_FRIEND_API_PATH}"
        friendPayload = {
            "user_id": friendId,              # QQ user ID to send message to
            "message": selectedPrivateMessage # Pre-formatted message structure
        }
        requestsList.append((friendUrl, friendPayload))
    
    return requestsList

# =============================================================================
# MESSAGE SENDING
# =============================================================================

def NapcatMessageSender(eventType: str) -> None:
    """
    Sends stream notifications to NapCat with robust retry logic.
    
    This function runs asynchronously (in a separate thread) to avoid blocking
    the main webhook server. It handles network failures gracefully by retrying
    failed requests and provides detailed logging for debugging.
    
    Args:
        eventType: Type of stream event that triggered this notification
        
    Error Handling:
        - Retries failed HTTP requests up to NAPCAT_RETRY_ATTEMPTS times
        - Waits NAPCAT_RETRY_DELAY seconds between retry attempts
        - Continues processing other requests even if some fail
        - Provides detailed error logging for debugging
    """
    print(f"(Async) Attempting to send NapCat notification for room {ROOM_ID} with event type: {eventType}...")  
    
    # Build all HTTP requests for this event
    requestsList = NapcatRequestConstructor(eventType)
    totalRequests = len(requestsList)
    successCount = 0
    
    print(f"(Async) Preparing to send {totalRequests} NapCat requests...")
    
    # Process each request individually with retry logic
    # This approach ensures that one failed request doesn't stop others from being sent
    for requestIndex, (fullNapcatUrl, payload) in enumerate(requestsList, 1):
        print(f"(Async) Processing request {requestIndex}/{totalRequests} to {fullNapcatUrl}...")
        lastErrorInfo = "Unknown error"
        requestSuccess = False

        # Retry loop for handling temporary network failures
        for attempt in range(NAPCAT_RETRY_ATTEMPTS):
            try:
                print(f"(Async) Request {requestIndex}/{totalRequests} send attempt #{attempt + 1}/{NAPCAT_RETRY_ATTEMPTS}...")
                
                # Send HTTP POST request to NapCat
                # Timeout prevents hanging on network issues
                response = requests.post(fullNapcatUrl, json=payload, timeout=10)
                
                if response.status_code == 200:
                    print(f"(Async) Request {requestIndex}/{totalRequests} sent successfully.")
                    requestSuccess = True
                    successCount += 1
                    break  # Success! No need to retry this request
                else:
                    # Log non-200 HTTP responses for debugging
                    lastErrorInfo = f"HTTP status code {response.status_code}: {response.text}"
                    
            except requests.exceptions.RequestException as exception:
                # Handle network errors, timeouts, connection failures, etc.
                lastErrorInfo = str(exception)
           
            # Wait before retrying (except on the last attempt)
            if attempt < NAPCAT_RETRY_ATTEMPTS - 1:
                print(f"(Async) Waiting {NAPCAT_RETRY_DELAY} seconds before retrying request {requestIndex}/{totalRequests}...")
                time.sleep(NAPCAT_RETRY_DELAY)
        
        # Log final failure if all retries exhausted
        if not requestSuccess:
            print(f"Error (Request {requestIndex}/{totalRequests} send failed): All {NAPCAT_RETRY_ATTEMPTS} attempts failed. Last error: {lastErrorInfo}")
    
    # Provide summary of notification sending results
    print(f"(Async) NapCat message sending completed: {successCount}/{totalRequests} requests sent successfully.")

# =============================================================================
# WEBHOOK EVENT HANDLING
# =============================================================================

@BREC_HOOK.route(WEBHOOK_ENDPOINT_PATH, methods=['POST'])
def WebhookRequestListener():
    """
    Flask route handler for receiving webhook events from BililiveRecorder.
    
    This function processes HTTP POST requests sent by BREC when stream events occur.
    It filters events to only handle our target room and triggers appropriate notifications.
    
    BREC Webhook Documentation: https://rec.danmuji.org/reference/webhook/
    
    Expected Webhook Payload Format:
        {
            "EventType": "StreamStarted" | "StreamEnded" | ...,
            "EventTimestamp": "2022-05-17T18:17:29.1604718+08:00",
            "EventId": "uuid-string",
            "EventData": {
                "RoomId": 1921712061,
                "Name": "Streamer Name",
                "Title": "Stream Title",
                ...
            }
        }
    
    Returns:
        HTTP 200 "OK" for successful processing
        HTTP 400 "Error" for malformed requests
    """
    try:
        # Parse JSON payload from BREC
        data_ = request.get_json()
        eventType = data_.get('EventType')
        eventData_ = data_.get('EventData')

        # Handle stream start events
        if eventType == 'StreamStarted' and \
           int(eventData_.get('RoomId')) == ROOM_ID:
           
            print(f"Target room {ROOM_ID} live stream started, sending NapCat message asynchronously.")
            
            # Use threading to avoid blocking the webhook server
            # This ensures BREC gets a quick response while notifications are processed in background
            napcatMessageThread = threading.Thread(target=NapcatMessageSender, args=("STREAMING_START",))
            napcatMessageThread.daemon = True   # Dies when main program exits
            napcatMessageThread.start()
           
            return "OK", 200
        
        # Handle stream end events  
        elif eventType == 'StreamEnded' and \
             int(eventData_.get('RoomId')) == ROOM_ID:
           
            print(f"Target room {ROOM_ID} live stream ended, sending NapCat message asynchronously.")
            
            # Same async processing pattern as stream start
            napcatMessageThread = threading.Thread(target=NapcatMessageSender, args=("STREAMING_ENDED",))
            napcatMessageThread.daemon = True
            napcatMessageThread.start()
           
            return "OK", 200
       
        # Acknowledge other events (different rooms, other event types) but don't process them
        return "OK", 200
        
    except Exception as exception:
        # Log malformed webhook requests for debugging
        originalDataStr = ""
        try:
            originalDataStr = request.data.decode('utf-8', errors='replace')
        except Exception:
            pass
        print(f"Error (Processing BREC webhook info problem): Processing error occurred - {str(exception)}. Original request body: '{originalDataStr}'")
        return "Error: Malformed request or data from recorder", 400

# =============================================================================
# WEBHOOK SERVER MANAGEMENT
# =============================================================================

def WebhookListenerServiceStarter() -> None:
    """
    Starts the webhook server in a background thread with health checking.
    
    This function:
    1. Creates a daemon thread to run the Flask/Waitress server
    2. Starts the thread and waits for it to initialize
    3. Performs a health check to ensure the server started successfully
    4. Exits the program if the server fails to start
    
    Threading Details:
        - Uses daemon=True so the server thread dies when main program exits
        - Server runs on a separate thread to avoid blocking BREC startup
        - Health check prevents silent failures during startup
        
    Waitress vs Flask Dev Server:
        - Waitress is a production WSGI server (no development warnings)
        - Better performance and reliability than Flask's built-in server
        - Handles concurrent requests properly
    """
    print(f"Initializing webhook request listening service...")
    
    def run_webhook_server():
        """Nested function that actually runs the Waitress WSGI server."""
        print(f"Webhook request listening service thread started. Ready to listen for BREC requests at http://{WEBHOOK_SERVER_HOST}:{WEBHOOK_SERVER_PORT}{WEBHOOK_ENDPOINT_PATH}...")
        try:
            # Use Waitress production WSGI server instead of Flask's development server
            # This eliminates the "development server" warning and provides better performance
            serve(BREC_HOOK, host=WEBHOOK_SERVER_HOST, port=WEBHOOK_SERVER_PORT)
        except Exception as exception:
            print(f"Webhook request listening service run failed: {exception}")
    
    # Create and start the webhook server thread
    webhookListenerThread = threading.Thread(target=run_webhook_server, daemon=True, name="WebhookListenerThread")
    webhookListenerThread.start()

    # Health check: wait a moment then verify the thread is still alive
    time.sleep(2)
   
    if not webhookListenerThread.is_alive():
        print(f"Error: Webhook request listening service thread failed to start successfully. Please check if port {WEBHOOK_SERVER_PORT} is occupied or configuration is incorrect.")
        exit(1)
    else:
        print(f"Webhook request listening service thread started successfully.")

# =============================================================================
# BREC PROCESS MANAGEMENT
# =============================================================================

def BrecStarter(cliPath: str, commandArgs: List[str]) -> None:
    """
    Starts BililiveRecorder as a subprocess and monitors its output.
    
    This function:
    1. Expands the CLI path (handles ~ for home directory)
    2. Starts BREC as a subprocess with the specified arguments
    3. Streams BREC's output to our console for monitoring
    4. Blocks until BREC exits (this is the main program loop)
    
    Args:
        cliPath: Path to BililiveRecorder.Cli executable
        commandArgs: List of command-line arguments to pass to BREC
        
    BREC Process Details:
        - BREC runs continuously monitoring the target room
        - Sends webhook notifications when stream events occur
        - Records video streams automatically (if configured)
        - Provides web UI for remote management
    """
    global BREC_PROCESS
    expandedCliPath = os.path.expanduser(cliPath)  # Convert ~ to actual home directory path
    command = [expandedCliPath] + commandArgs
   
    try:
        # Start BREC as subprocess with output piping
        BREC_PROCESS = subprocess.Popen(
            command, 
            stdout=subprocess.PIPE,      # Capture stdout 
            stderr=subprocess.STDOUT,    # Merge stderr into stdout
            text=True,                   # Handle strings instead of bytes
            bufsize=1,                   # Line buffering
            universal_newlines=True      # Handle line endings properly
        )
        
        # Stream BREC's output to our console in real-time
        # This allows monitoring BREC's status and debugging issues
        if BREC_PROCESS.stdout:
            for line in BREC_PROCESS.stdout:
                print(line.strip())  # Remove trailing newlines
       
        # Wait for BREC to exit (this blocks until the process ends)
        BREC_PROCESS.wait()
           
    except Exception as exception:
        print(f"BREC startup or runtime error: {exception}")
    finally:
        pass

# =============================================================================
# SIGNAL HANDLING (GRACEFUL SHUTDOWN)
# =============================================================================

def SignalProcessor(signalNumber: int, stackFrame) -> None:
    """
    Handles interrupt signals (Ctrl+C, SIGTERM) for graceful program shutdown.
    
    This function:
    1. Catches interrupt signals before the program dies
    2. Cleanly terminates the BREC subprocess
    3. Allows the webhook server thread to die naturally (daemon=True)
    4. Exits the program cleanly
    
    Args:
        signalNumber: The signal number that was received (e.g., SIGINT=2, SIGTERM=15)
        stackFrame: Stack frame information (unused but required by signal handlers)
        
    Graceful Shutdown Process:
        1. Try to terminate BREC nicely (SIGTERM)
        2. Wait up to 5 seconds for BREC to exit
        3. Force kill BREC if it doesn't exit (SIGKILL)
        4. Exit the main program (daemon threads die automatically)
    """
    print(f"\nInterrupt signal {signalNumber} detected, attempting to close program...")
    global BREC_PROCESS

    # Clean up BREC subprocess if it's still running
    if BREC_PROCESS and BREC_PROCESS.poll() is None:
        print(f"Attempting to terminate BREC process...")
        BREC_PROCESS.terminate()  # Send SIGTERM (polite request to exit)
        
        try:
            # Wait up to 5 seconds for BREC to exit gracefully
            BREC_PROCESS.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # BREC didn't exit within timeout, force kill it
            print(f"BREC process did not terminate within 5 seconds, force killing (SIGKILL)...")
            BREC_PROCESS.kill()  # Send SIGKILL (immediate termination)
            
        print(f"BREC process handled.")
   
    print(f"Program exiting.")
    exit(0)

# =============================================================================
# MAIN PROGRAM ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    """
    Main program execution flow:
    
    1. Set up signal handlers for graceful shutdown (Ctrl+C support)
    2. Validate that BREC executable exists and is runnable
    3. Start the webhook server in background thread
    4. Start BREC process (this blocks until BREC exits)
    5. Clean up any remaining processes on exit
    
    Program Architecture:
        Main Thread: Runs BREC subprocess, handles signals
        Webhook Thread: Runs Flask/Waitress server (daemon)
        Notification Threads: Send HTTP requests to NapCat (daemon, short-lived)
    
    Prerequisites:
        - BililiveRecorder installed at BREC_CLI_PATH
        - NapCat running and accessible at NAPCAT_BASE_URL
        - Configuration file 'napcat_config.json' exists and is valid
        - Target Bilibili room exists and is accessible
    """
    
    # Set up signal handlers for graceful shutdown
    # This allows users to press Ctrl+C to cleanly stop the program
    signal.signal(signal.SIGINT, SignalProcessor)   # Handle Ctrl+C
    signal.signal(signal.SIGTERM, SignalProcessor)  # Handle kill command

    # Validate BREC installation before proceeding
    actualCliPath = os.path.expanduser(BREC_CLI_PATH)
    if not (os.path.exists(actualCliPath) and os.access(actualCliPath, os.X_OK)):
        print(f"Error: BREC program path '{actualCliPath}' (from config '{BREC_CLI_PATH}') does not exist or is not executable.")
        print("Please install BililiveRecorder and update BREC_CLI_PATH in this script.")
        print("Download: https://github.com/BililiveRecorder/BililiveRecorder")
        exit(1)

    # Start the webhook server in background thread
    # This must be running before BREC starts so it can receive webhook notifications
    WebhookListenerServiceStarter()

    # Start BREC process (this is the main program loop)
    # BREC will run continuously until stopped by user or error
    try:
        BrecStarter(actualCliPath, BREC_ARGS)
    finally:
        # Emergency cleanup: ensure BREC is stopped even if something goes wrong
        if BREC_PROCESS and BREC_PROCESS.poll() is None:
            print(f"Before script exit, attempting to close still running BREC process...")
            BREC_PROCESS.terminate()
            try:
                BREC_PROCESS.wait(timeout=2)
            except subprocess.TimeoutExpired:
                BREC_PROCESS.kill()
        print(f"Script execution flow ended.")
