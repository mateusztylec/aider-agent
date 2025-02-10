import os
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional
import git
import requests
from fastapi import HTTPException, APIRouter
import logging

from aider.api_models import InstructionRequest
from aider.api_helpers import conversation

logger = logging.getLogger(__name__)

# Create the agent router with prefix
agent_router = APIRouter(prefix="/agent")

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

    # If directory exists, remove it to ensure clean state
    if app_dir.exists():
        logger.debug(f"Removing existing directory: {app_dir}")
        import shutil
        shutil.rmtree(app_dir)

    logger.debug(f"Creating directory: {app_dir}")
    app_dir.mkdir(parents=True)

    try:
        # Initialize new repository
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


@agent_router.post("/instruction")
async def agent_instruction(request: InstructionRequest):
    instruction = request.instruction
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

        # Initialize aider and start conversation
        from aider.api_aider import initialize_aider_api, InitRequest
        initialize_aider_api(InitRequest(pretty=False))
        conversation(instruction)

        repo.git.push('--set-upstream', 'origin', branch)

        # Create pull request if token is provided and it's a GitHub repository
        if token and "github.com" in repo_url:
            return create_github_pull_request(repo_url, branch, token, instruction)

        return {"status": "success", "message": f"Repository ready on branch {branch}"}

    except git.exc.GitCommandError as e:
        raise HTTPException(status_code=500, detail=f"Git operation failed: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Operation failed: {str(e)}")


@agent_router.post("/instruction-test")
async def agent_instruction_test(request: InstructionRequest):
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

        # Initialize aider and start conversation
        from aider.api_aider import initialize_aider_api, InitRequest
        initialize_aider_api(InitRequest(pretty=False))
        conversation(request.instruction)

        current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

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
