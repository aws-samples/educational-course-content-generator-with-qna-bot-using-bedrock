## Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
## SPDX-License-Identifier: LicenseRef-.amazon.com.-AmznSL-1.0
## Licensed under the Amazon Software License  https://aws.amazon.com/asl/
from pydantic import BaseModel, Field
from typing import List

class VideoScript(BaseModel):
    """
    Represents a video script for a sub-learning outcome. Each video script should cover key concepts of the sub-outcome.
    """
    script: str = Field(..., description="A video script approximately 3 minutes long, covering key concepts of the sub-learning outcome.")

class ReadingMaterial(BaseModel):
    """
    Represents the reading material for a main learning outcome. This should provide additional context and in-depth coverage of the main concepts.
    """
    title: str = Field(..., description="Title of the reading material related to the main learning outcome.")
    content: str = Field(..., description="Detailed reading material content, at least one page long, providing in-depth information on the main learning outcome.")

class MultipleChoiceQuestion(BaseModel):
    """
    Represents a multiple-choice question related to a sub-learning outcome, designed to test understanding after watching the associated video.
    """
    question: str = Field(..., description="The multiple-choice question for the sub-learning outcome.")
    options: List[str] = Field(..., description="A list of answer options for the question.")
    correct_answer: str = Field(..., description="The correct answer for the multiple-choice question.")

class SubLearningOutcomeContent(BaseModel):
    """
    Represents all content associated with a specific sub-learning outcome, including a video script and a related multiple-choice question.
    """
    sub_learning_outcome: str = Field(..., description="The sub-learning outcome, representing a specific objective that supports the main learning outcome.")
    video_script: VideoScript = Field(..., description="A 3-minute video script explaining the sub-learning outcome.")
    multiple_choice_question: MultipleChoiceQuestion = Field(..., description="A multiple-choice question testing the sub-learning outcome.")

class CourseContent(BaseModel):
    """
    Represents all content for a specific week in the course, including the main learning outcome, reading materials, and sub-learning outcomes with supporting materials.
    """
    week_number: int = Field(..., description="The specific week number in the course.")
    main_learning_outcome: str = Field(..., description="The main learning outcome for this week, representing the core learning objective.")
    reading_material: ReadingMaterial = Field(..., description="A detailed reading material providing comprehensive coverage of the main learning outcome.")
    sub_learning_outcomes_content: List[SubLearningOutcomeContent] = Field(..., description="A list of sub-learning outcomes and their associated content, including video scripts and questions.")

