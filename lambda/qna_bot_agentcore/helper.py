## Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
## SPDX-License-Identifier: LicenseRef-.amazon.com.-AmznSL-1.0
## Licensed under the Amazon Software License  https://aws.amazon.com/asl/


def build_retrieve_filter(course_name, course_id, week_number):
    """Build the retrieval filter condition for the Bedrock Knowledge Base.

    Constructs a filter dictionary compatible with the Strands retrieve tool's
    retrieveFilter parameter for metadata-based filtering of KB results.

    Args:
        course_name: Name of the course to filter by.
        course_id: Unique identifier for the course.
        week_number: Maximum week number to include (inclusive).

    Returns:
        A filter dictionary for the retrieve tool, or None if no filters apply.
    """
    and_all_conditions = []

    if course_name and course_name != "":
        and_all_conditions.append(
            {"equals": {"key": "course_name", "value": course_name}}
        )

    if course_id and course_id != "":
        and_all_conditions.append(
            {"equals": {"key": "course_id", "value": course_id}}
        )

    if week_number is not None and week_number != "":
        and_all_conditions.append(
            {"lessThanOrEquals": {"key": "week", "value": int(week_number)}}
        )

    if len(and_all_conditions) >= 2:
        return {"andAll": and_all_conditions}
    elif len(and_all_conditions) == 1:
        return and_all_conditions[0]
    else:
        return None


def build_system_prompt(course_name=None, learning_objective=None):
    """Build a context-aware system prompt for the QnA agent.

    Args:
        course_name: Optional course name for contextual responses.
        learning_objective: Optional learning objective for focused responses.

    Returns:
        A system prompt string for the Strands Agent.
    """
    base_prompt = """You are an academic question answering assistant for an educational platform.

Your primary responsibilities:
1. Use the retrieve tool to search the knowledge base for relevant course materials before answering any question.
2. Answer student questions based ONLY on the retrieved course content.
3. If the retrieved results don't contain relevant information, clearly state that you cannot find a definitive answer in the course materials.
4. Verify any claims made by the student against the retrieved content before confirming them.
5. Do not provide information beyond what is found in the course materials.

Response format:
- Respond in clean Markdown format suitable for direct rendering on a website.
- Start your response directly with the answer content. Do NOT include any preamble, commentary about the search process, or phrases like "Great news!", "Here's what I found", "Based on the course materials", etc.
- Use Markdown headings (##, ###), bullet points, bold text, and other formatting to structure the answer clearly.
- Keep the tone educational, supportive, and concise.
- Reference specific course materials when possible.
- If a question is ambiguous, ask for clarification."""

    context_parts = []
    if course_name:
        context_parts.append(f"You are currently assisting with the course: '{course_name}'.")
    if learning_objective:
        context_parts.append(f"The current learning objective is: '{learning_objective}'.")

    if context_parts:
        context = "\n".join(context_parts)
        return f"{base_prompt}\n\nCurrent Context:\n{context}"

    return base_prompt
