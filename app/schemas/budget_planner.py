from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SimulateRequest(BaseModel):
    """POST /budget/simulate request body -- exactly the fields this
    step's ask names."""

    target_city: str = Field(min_length=1, max_length=255)
    target_country: str = Field(min_length=1, max_length=255)
    monthly_stipend: float = Field(default=0.0, ge=0)


class ExpenseEstimateOut(BaseModel):
    low: float
    high: float
    notes: str | None


class EstimatedExpensesOut(BaseModel):
    rent_student_housing: ExpenseEstimateOut
    groceries: ExpenseEstimateOut
    public_transport: ExpenseEstimateOut
    health_insurance: ExpenseEstimateOut
    utilities_and_internet: ExpenseEstimateOut
    leisure_and_pocket_money: ExpenseEstimateOut


class FinancialHealthStatusOut(BaseModel):
    status: Literal["surplus", "balanced", "deficit"]
    analysis: str


class LegalStudentJobOut(BaseModel):
    job_title: str
    hourly_wage_low: float
    hourly_wage_high: float
    legal_weekly_hour_limit: int
    description: str


class GeneratedBudgetDataOut(BaseModel):
    """Mirrors `app.services.budget_service.GeneratedBudgetData` -- kept
    as a separate schema-layer copy, same rationale already established
    in `app/schemas/parent_pitch.py`'s `GeneratedPitchData` and
    `app/schemas/decision_matrix.py`'s `MatrixResultOut`."""

    currency: str
    estimated_expenses: EstimatedExpensesOut
    financial_health_status: FinancialHealthStatusOut
    legal_student_jobs: list[LegalStudentJobOut]
    local_saving_hacks: list[str]


class BudgetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    target_city: str
    target_country: str
    monthly_stipend: float
    generated_budget_data: GeneratedBudgetDataOut
    created_at: datetime
