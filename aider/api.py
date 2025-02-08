from fastapi import FastAPI, HTTPException
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

from aider.io import InputOutput, get_rel_fname  # Import original class and function

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


class InitRequest(BaseModel):
    pretty: bool = False


class Message(BaseModel):
    content: str


def set_aider_args(args):
    """Set the aider arguments before starting the server"""
    global AIDER_ARGS
    AIDER_ARGS = args + ["--no-stream", "--config", "/home/appuser/.aider.conf.yml"]


@app.post("/init")
async def initialize_aider(request: InitRequest):
    from aider.main import main

    app.io = APIInputOutput(pretty=request.pretty)
    app.io.coder = main(AIDER_ARGS, input=None, output=None, return_coder=True, io=app.io)
    return {"status": "initialized", "message": "Aider initialized successfully"}


@app.post("/chat")
async def chat(message: Message):
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


@app.post("/stop")
async def stop_aider():
    if hasattr(app, "io"):
        app.io.input_queue.put("exit")
    return {"status": "stopped"}


##

# AGENT API

def generate_branch_name() -> str:
    """Generate a unique branch name based on current timestamp."""
    current_time = datetime.now()
    return f"cool-agent-{current_time.strftime('%Y%m%d-%H%M%S')}"


def get_repo_url_with_token(repo_url: str, token: Optional[str]) -> str:
    """Add token to repository URL if provided."""
    if token:
        return repo_url.replace("https://", f"https://{token}@")
    return repo_url


def setup_repository(app_dir: Path, repo_url: str, branch: str) -> git.Repo:
    """Initialize or update repository and create new branch."""
    logger.debug(f"Setting up repository in {app_dir}")
    logger.debug(f"Branch to create: {branch}")

    if not app_dir.exists():
        logger.debug(f"Creating directory: {app_dir}")
        app_dir.mkdir(parents=True)

    try:
        if not (app_dir / ".git").exists():
            logger.debug("Initializing new git repository")
            repo = git.Repo.init(app_dir)
            logger.debug(f"Adding remote origin: {repo_url}")
            origin = repo.create_remote("origin", repo_url)
            logger.debug("Fetching from origin")
            origin.fetch()

            # Check available branches
            logger.debug("Available remote branches:")
            for ref in repo.remote().refs:
                logger.debug(f"- {ref.name}")

            try:
                logger.debug("Attempting to create branch from origin/main")
                repo.git.checkout("origin/main", b=branch)
            except git.exc.GitCommandError as e:
                logger.debug(f"Failed to checkout main: {e}")
                logger.debug("Attempting to create branch from origin/master")
                repo.git.checkout("origin/master", b=branch)
        else:
            logger.debug("Repository exists, creating new branch")
            repo = git.Repo(app_dir)
            logger.debug("Fetching from origin")
            repo.remotes.origin.fetch()

            # Check current state
            logger.debug(f"Current branch: {repo.active_branch.name}")
            logger.debug("Local branches:")
            for b in repo.heads:
                logger.debug(f"- {b.name}")

            logger.debug(f"Creating and checking out new branch: {branch}")
            new_branch = repo.create_head(branch)
            new_branch.checkout()

        # Add push step with more detailed error handling
        try:
            logger.debug(f"Pushing branch {branch} to origin")
            # First check if we have any changes to commit
            if repo.is_dirty():
                logger.debug("Repository has uncommitted changes")
                repo.git.add('.')
                repo.git.commit('-m', 'Initial commit')
                logger.debug("Changes committed")

            # Check if branch exists on remote
            remote_branches = [ref.name for ref in repo.remote().refs]
            logger.debug(f"Remote branches before push: {remote_branches}")

            repo.git.push('--set-upstream', 'origin', branch)
            logger.debug("Branch pushed successfully")

            # Verify push
            repo.remote().fetch()
            remote_branches_after = [ref.name for ref in repo.remote().refs]
            logger.debug(f"Remote branches after push: {remote_branches_after}")

        except git.exc.GitCommandError as e:
            logger.error(f"Failed to push branch: {e}")
            logger.error(f"Git command output: {e.stdout}")
            logger.error(f"Git command error: {e.stderr}")
            raise

        return repo

    except Exception as e:
        logger.exception("Unexpected error in setup_repository")
        raise


def create_github_pull_request(
    repo_url: str,
    branch: str,
    token: str,
    instruction: str
) -> Dict[str, Any]:
    """Create a pull request on GitHub."""
    logger.debug(f"Starting PR creation for branch: {branch}")
    logger.debug(f"Repository URL: {repo_url.replace(token, '***') if token else repo_url}")

    # Extract owner and repo name from repo URL
    parts = repo_url.split('github.com/')[-1].replace('.git', '').split('/')
    owner, repo_name = parts[0], parts[1]

    # First, verify the branch exists
    verify_url = f"https://api.github.com/repos/{owner}/{repo_name}/branches/{branch}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json"
    }

    verify_response = requests.get(verify_url, headers=headers)
    if verify_response.status_code != 200:
        logger.error(f"Branch {branch} does not exist or is not accessible")
        return {
            "status": "error",
            "message": f"Branch {branch} does not exist or is not accessible"
        }

    # GitHub API endpoint for PR creation
    api_url = f"https://api.github.com/repos/{owner}/{repo_name}/pulls"

    # Prepare pull request data
    current_time = datetime.now()
    pr_data = {
        "title": f"Automated changes {current_time.strftime('%Y-%m-%d %H:%M:%S')}",
        "body": f"Automated pull request created by agent\n\nInstruction: {instruction}",
        "head": branch,
        "base": "main",  # Try main first
        "maintainer_can_modify": True
    }

    try:
        response = requests.post(api_url, json=pr_data, headers=headers)
        response_json = response.json()

        # If main branch failed, try master
        if response.status_code != 201 and "base" in str(response_json.get("errors", [])):
            pr_data["base"] = "master"
            response = requests.post(api_url, json=pr_data, headers=headers)
            response_json = response.json()

        result = {
            "status": "success" if response.status_code == 201 else "error",
            "message": f"Repository ready on branch {branch}"
        }

        if response.status_code == 201:
            logger.info(f"PR created successfully: {response_json['html_url']}")
            result["pull_request_url"] = response_json["html_url"]
        else:
            error_message = response_json.get('message', 'Unknown error')
            errors = response_json.get('errors', [])
            error_details = '; '.join([f"{e.get('message', '')}" for e in errors]) if errors else ''

            result["pull_request_error"] = f"Failed to create PR: {error_message}"
            if error_details:
                result["pull_request_error"] += f" - Details: {error_details}"

        return result

    except requests.exceptions.RequestException as e:
        logger.exception("Exception occurred while creating PR")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create PR due to request error: {str(e)}"
        )


class InstructionRequest(BaseModel):
    instruction: str


@app.post("/agent/instruction")
async def agent_instruction(instruction: str):
    if not os.getenv("REPO_URL"):
        raise HTTPException(
            status_code=400,
            detail="Environment variable REPO_URL must be set"
        )

    app_dir = Path("/app")
    repo_url = os.getenv("REPO_URL")
    token = os.getenv("GITHUB_TOKEN")

    try:
        # Generate branch name
        branch = generate_branch_name()

        # Add token to URL if provided
        full_repo_url = get_repo_url_with_token(repo_url, token)

        # Setup repository and create branch
        repo = setup_repository(app_dir, full_repo_url, branch)

        # Create pull request if token is provided and it's a GitHub repository
        if token and "github.com" in repo_url:
            return create_github_pull_request(repo_url, branch, token, instruction)

        return {"status": "success", "message": f"Repository ready on branch {branch}"}

    except git.exc.GitCommandError as e:
        raise HTTPException(status_code=500, detail=f"Git operation failed: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Operation failed: {str(e)}")


@app.post("/agent/instruction-test")
async def agent_instruction_test(request: InstructionRequest):
    from dotenv import load_dotenv

    load_dotenv()
    instruction = request.instruction
    if not os.getenv("REPO_URL"):
        raise HTTPException(
            status_code=400,
            detail="Environment variable REPO_URL must be set"
        )

    app_dir = Path("./temp")
    repo_url = os.getenv("REPO_URL")
    token = os.getenv("GITHUB_TOKEN")

    try:
        # Generate branch name
        branch = generate_branch_name()

        # Add token to URL if provided
        full_repo_url = get_repo_url_with_token(repo_url, token)

        # Setup repository and create branch
        repo = setup_repository(app_dir, full_repo_url, branch)

        # Add a simple commit for testing
        logger.debug("Adding test commit to README.md")
        readme_path = app_dir / "README.md"
        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        with open(readme_path, 'a') as f:
            f.write(f"\nTest update by agent at {current_time}\n")

        repo.index.add(['README.md'])
        repo.index.commit(f"Test commit: Update README.md at {current_time}")
        repo.git.push('--set-upstream', 'origin', branch)
        logger.debug("Test commit pushed successfully")

        # Create pull request if token is provided and it's a GitHub repository
        if token and "github.com" in repo_url:
            return create_github_pull_request(repo_url, branch, token, instruction)

        return {"status": "success", "message": f"Repository ready on branch {branch}"}

    except git.exc.GitCommandError as e:
        raise HTTPException(status_code=500, detail=f"Git operation failed: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Operation failed: {str(e)}")
