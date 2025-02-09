from fastapi import FastAPI, HTTPException, APIRouter
from pydantic import BaseModel
from queue import Queue
import sys
import os
from datetime import datetime
import git
from pathlib import Path
from typing import Optional, Dict, Any
import requests
import logging

from aider.io import InputOutput, get_rel_fname
from aider.api_models import InitRequest, Message
from aider.api_agent import agent_router

app = FastAPI()

# Store aider args globally so they can be set before starting uvicorn
AIDER_ARGS = []

logger = logging.getLogger(__name__)


class GitRepoConfig(BaseModel):
    repo_url: str
    branch: str
    token: Optional[str] = None

# AIDER API


class APIInputOutput(InputOutput):
    def __init__(self, pretty=False):
        # Initialize base class with minimum required parameters
        super().__init__(
            pretty=pretty,
            yes=True,  # Automatically confirm all questions
            input_history_file=None,
            chat_history_file=None,
            encoding='utf-8'
        )
        self.current_response = []
        self.input_queue = Queue()
        self.coder = None
        self.current_files_info = None
        self.current_edit_format = None
        self.files_added_in_current_chat = 0  # Counter for files added in current chat session

    def update_files_status(self):
        """Update and add files status to current response if available"""
        if self.coder:
            rel_fnames = self.coder.get_inchat_relative_files()
            rel_read_only_fnames = [get_rel_fname(fname, self.coder.root)
                                    for fname in self.coder.abs_read_only_fnames]

            self.current_files_info = self.format_files_for_input(rel_fnames, rel_read_only_fnames)
            if self.current_edit_format:
                self.current_files_info["edit_format"] = self.current_edit_format

            self.current_response.append({
                "type": "files_status",
                "files": self.current_files_info
            })

    def format_files_for_input(self, rel_fnames, rel_read_only_fnames):
        """Format file information for API response"""
        read_only_files = sorted(rel_read_only_fnames or [])
        editable_files = [f for f in sorted(rel_fnames) if f not in rel_read_only_fnames]

        # Use shorter paths for read-only files
        ro_paths = []
        for rel_path in read_only_files:
            abs_path = os.path.abspath(os.path.join(self.root, rel_path))
            ro_paths.append(abs_path if len(abs_path) < len(rel_path) else rel_path)

        return {
            "read_only_files": ro_paths,
            "editable_files": editable_files
        }

    # Override interactive methods while maintaining response collection logic
    def tool_output(self, message="", log_only=False, bold=False):
        if not log_only:
            response = {"type": "tool_output", "message": str(message)}
            self._parse_tokens_and_costs(message, response)
            self.current_response.append(response)
        # Call original method to maintain logging
        super().append_chat_history(message, linebreak=True, blockquote=True)

    def _parse_tokens_and_costs(self, message, response):
        """Helper method for parsing token and cost information"""
        import re
        pattern = r"Tokens:\s*([\d\.kM]+)\s*sent,\s*([\d\.kM]+)\s*received\. Cost:\s*\$([\d\.]+)\s*message,\s*\$([\d\.]+)\s*session\."
        match = re.search(pattern, message)
        if match:
            tokens_sent_str, tokens_received_str, cost_message, cost_session = match.groups()

            def parse_tokens(token_str):
                token_str = token_str.lower()
                if 'k' in token_str:
                    return float(token_str.replace('k', '')) * 1000
                elif 'm' in token_str:
                    return float(token_str.replace('m', '')) * 1_000_000
                return float(token_str)

            response.update({
                "tokens_sent": str(int(parse_tokens(tokens_sent_str))),
                "tokens_received": str(int(parse_tokens(tokens_received_str))),
                "cost_message": cost_message,
                "cost_session": cost_session
            })
            # Add file information only after token information
            self.update_files_status()

    def tool_error(self, message="", strip=True):
        self.current_response.append({"type": "error", "message": str(message)})
        super().append_chat_history(message, linebreak=True, blockquote=True)

    def tool_warning(self, message="", strip=True):
        self.current_response.append({"type": "warning", "message": str(message)})
        super().append_chat_history(message, linebreak=True, blockquote=True)

    def get_input(self, root, rel_fnames, addable_rel_fnames, commands, abs_read_only_fnames=None, edit_format=None):
        # Reset file counter for each new input
        self.files_added_in_current_chat = 0

        # Update edit format
        self.current_edit_format = edit_format

        # Update file information
        rel_read_only_fnames = [get_rel_fname(fname, root)
                                for fname in (abs_read_only_fnames or [])]
        self.current_files_info = self.format_files_for_input(rel_fnames, rel_read_only_fnames)

        if edit_format:
            self.current_files_info["edit_format"] = edit_format

        self.current_response.append({
            "type": "files_status",
            "files": self.current_files_info
        })

        return self.input_queue.get()

    def assistant_output(self, message, pretty=None):
        self.current_response.append({
            "type": "assistant",
            "message": str(message),
            "timestamp": datetime.now().isoformat()
        })
        super().append_chat_history(message, linebreak=True)

    def confirm_ask(self, question, default="y", subject=None, explicit_yes_required=False, group=None, allow_never=False):
        self.current_response.append({
            "type": "confirm",
            "question": question,
            "answer": "Yes",
            "subject": subject
        })

        # Check if the question is about adding a file
        if "Do you want to create" in question or "Add" in question and "to the chat" in question:
            if self.files_added_in_current_chat >= 4:
                self.tool_warning(
                    f"File limit of 4 files per chat session exceeded. Rejecting additional file.")
                return False
            self.files_added_in_current_chat += 1
            return True

        return True  # Always confirm for other questions

    def prompt_ask(self, question, default="", subject=None):
        self.current_response.append({
            "type": "prompt",
            "question": question,
            "subject": subject
        })
        return default


def set_aider_args(args):
    """Set the aider arguments before starting the server"""
    global AIDER_ARGS
    if os.getenv("NODE_ENV") == "development":
        AIDER_ARGS = args + ["--no-stream", "--config", "./docker/.aider.conf.yml"]
    else:
        AIDER_ARGS = args + ["--no-stream", "--config", "/home/appuser/.aider.conf.yml"]


def initialize_aider_api(request: InitRequest):
    from aider.main import main

    app.io = APIInputOutput(pretty=request.pretty)
    app.io.coder = main(AIDER_ARGS, input=None, output=None, return_coder=True, io=app.io)
    return {"status": "initialized", "message": "Aider initialized successfully"}


def chat_with_aider_api(message):
    if not hasattr(app, "io") or not app.io.coder:
        raise HTTPException(status_code=400, detail="Aider not initialized")

    app.io.current_response = []  # Clear previous responses
    app.io.input_queue.put(message.content)

    try:
        app.io.coder.run(with_message=message.content)
        return {
            "status": "success",
            "responses": app.io.current_response
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "responses": app.io.current_response
        }


@app.post("/init")
async def initialize_aider(request: InitRequest):
    return initialize_aider_api(request)


@app.post("/chat")
async def chat(message: Message):
    return chat_with_aider_api(message)


@app.post("/stop")
async def stop_aider():
    if hasattr(app, "io"):
        app.io.input_queue.put("exit")
    return {"status": "stopped"}

# Import and include agent router after all app routes are defined
app.include_router(agent_router)
