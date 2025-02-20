## Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
## SPDX-License-Identifier: LicenseRef-.amazon.com.-AmznSL-1.0
## Licensed under the Amazon Software License  https://aws.amazon.com/asl/
from pydantic import BaseModel, Field
from typing import List, Optional

class SubOutcome(BaseModel):
    """
    Represents a sub-learning outcome.
    """
    sub_outcome: str = Field(..., description="A supporting sub-learning outcome.")

class MainOutcome(BaseModel):
    """
    Represents a main learning outcome with supporting sub-learning outcomes.
    """
    outcome: str = Field(..., description="A main learning outcome.")
    sub_outcomes: List[str] = Field(..., description="A list of supporting sub-learning outcomes for the main outcome.")

class WeeklyOutline(BaseModel):
    """
    Represents the outline for a specific week, including main learning outcomes.
    """
    week: int = Field(..., description="Week number.")
    main_outcomes: List[MainOutcome] = Field(..., description="A list of main learning outcomes for the week.")

class CourseOutline(BaseModel):
    """
    Represents the entire course outline including the course title, duration, and weekly breakdown.
    """
    course_title: str = Field(..., description="Title of the course.")
    course_duration: str = Field(..., description="Duration of the course in weeks.")
    weekly_outline: List[WeeklyOutline] = Field(..., description="List of weekly outlines, including learning outcomes.")