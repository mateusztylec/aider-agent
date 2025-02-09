from langfuse.openai import OpenAI

import json
import os
from aider.api_models import Message

prompt = """
You act as a human that have a LLM code assistant called aider. Whenever you ask aider to do something, say that "I" want to do something.
You have AI code assistant called aider. Aider is a tool that helps you write code.
You have to guide aider to write code for you. Discuss with aider what files to edit.
Based on the ongoing conversation decide what to do next.
Precisely check the previous user messages as this is output from aider.
User message is a aider response, not human!!

Your response should be in the following single JSON format:

{{
    "type": "aider",
    "content": "command or text to aider"
}}

You have to follow these instructions to use with aider:
<aider_information>
Aider is a CLI tool (but rewritten to API) that helps you write code.
By default, aider have map of the repository in its memory with some function names.
Aider is USUALLY not able to edit files by himself. It usually needs to be added to the chat memory by you.

Take a moment and think about which files will need to be changed. Aider can often figure out which files to edit all by itself, but the most efficient approach is for you to add the files to the chat.

Just add the files you think need to be edited. Too much irrelevant code will distract and confuse the LLM. Aider uses a map of your entire git repo so is usually aware of relevant classes/functions/methods elsewhere in your code base.

Discuss a plan first

If you want aider to create a new file, add it to the repository first with /add <file>. This way aider knows this file exists and will write to it. Otherwise, aider might write the changes to an existing file. This can happen even if you ask for a new file, as LLMs tend to focus a lot on the existing information in their contexts.

Add only one command at a time. You cannot combine commands in the same message which means you cannot do '/add <file>' and 'some text' in the same message.
</aider_information>

<aider_instructions>
- normal text - normal text to conversate with aider about the project, files, edits and everything else.

/add <file_name> - add new file to the chat

/web <website_url> - Scrape a webpage, convert to markdown and send in a message

/commit - commit changes to the project

/quit - quit aider
</aider_instructions>


<human_goal>
{instruction}
</human_goal>

Respon with SINGLE JSON object. Without any other text. Based on the ongoing conversation.
"""


def extract_json_from_response(response_text: str) -> dict:
    # Try to find JSON between any XML-like tags first
    import re
    json_match = re.search(r'({.*})', response_text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except json.JSONDecodeError:
            pass

    # If no JSON found between tags or parsing failed, try to parse the whole text
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        raise ValueError("Could not extract valid JSON from response")


def conversation(instruction: str):
    messages = [
        {"role": "system", "content": prompt.format(instruction=instruction)},
    ]
    client = OpenAI(api_key=os.getenv("GROQ_API_KEY"), base_url="https://api.groq.com/openai/v1")

    for i in range(5):
        print(f"Iteration {i}")
        response = client.chat.completions.create(
            model="deepseek-r1-distill-llama-70b",
            messages=messages,
            name="aider-agent",
        )
        response_content = response.choices[0].message.content
        messages.append({"role": "assistant", "content": response_content})

        try:
            response_json = extract_json_from_response(response_content)
        except ValueError as e:
            print(f"Failed to parse response as JSON: {e}")
            continue

        if response_json["type"] == "aider":
            if response_json["content"] == "/quit":
                return
            else:
                # Import here to avoid circular imports
                from aider.api_aider import chat_with_aider_api
                aider_response = chat_with_aider_api(Message(content=response_json["content"]))
                messages.append({"role": "user", "content": json.dumps(aider_response)})
        elif response_json["type"] == "perplexity":
            messages.append({"role": "user", "content": str(response_json["content"])})

    return messages
