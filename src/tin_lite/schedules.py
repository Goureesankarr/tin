from __future__ import annotations

from copy import copy
from datetime import UTC, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleCalendarSpec,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleRange,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
)
from temporalio.service import RPCError, RPCStatusCode

from tin_lite.settings import Settings

WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


class ScheduledWorkflowSkip(RuntimeError):
    """An occurrence became stale or overlaps another accepted occurrence."""


class WorkflowSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cadence: Literal["daily", "weekly"]
    weekdays: list[
        Literal[
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
        ]
    ] = Field(default_factory=list, max_length=7)
    local_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: str = Field(min_length=1, max_length=100)
    start_at: AwareDatetime | None = None
    end_at: AwareDatetime | None = None

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be an IANA timezone name") from exc
        return value

    @model_validator(mode="after")
    def valid_weekdays(self) -> WorkflowSchedule:
        if self.start_at and self.end_at and self.end_at <= self.start_at:
            raise ValueError("schedule end must be after its start")
        self.weekdays = list(dict.fromkeys(self.weekdays))
        if self.cadence == "weekly" and not self.weekdays:
            raise ValueError("weekly schedules require at least one weekday")
        if self.cadence == "daily" and self.weekdays:
            raise ValueError("daily schedules do not accept weekdays")
        return self


def next_run_after(schedule: WorkflowSchedule, after: datetime | None = None) -> datetime | None:
    zone = ZoneInfo(schedule.timezone)
    threshold = (after or datetime.now(UTC)).astimezone(UTC)
    if schedule.start_at and threshold < schedule.start_at:
        threshold = schedule.start_at.astimezone(UTC) - timedelta(microseconds=1)
    cursor = threshold.astimezone(zone)
    hour, minute = (int(value) for value in schedule.local_time.split(":"))
    selected = set(schedule.weekdays)
    for days_ahead in range(0, 15):
        date = cursor.date() + timedelta(days=days_ahead)
        if schedule.cadence == "weekly" and WEEKDAYS[date.weekday()] not in selected:
            continue
        local = datetime.combine(date, time(hour, minute))
        # Match Temporal's calendar: omit nonexistent times, include both repeated times.
        candidates = sorted(
            {
                local.replace(tzinfo=zone, fold=fold).astimezone(UTC)
                for fold in (0, 1)
                if local.replace(tzinfo=zone, fold=fold)
                .astimezone(UTC)
                .astimezone(zone)
                .replace(tzinfo=None)
                == local
            }
        )
        for candidate in candidates:
            if candidate <= threshold:
                continue
            if schedule.end_at and candidate >= schedule.end_at:
                return None
            return candidate
    raise RuntimeError("schedule has no next occurrence")


class TemporalScheduleService:
    def __init__(self, *, client: Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def create(
        self, *, project_workflow_id: str, schedule: WorkflowSchedule, paused: bool = False
    ) -> str:
        schedule_id = self.schedule_id(project_workflow_id)
        definition = self._definition(
            project_workflow_id=project_workflow_id,
            schedule=schedule,
            paused=paused,
        )
        try:
            await self._client.create_schedule(schedule_id, definition)
        except ScheduleAlreadyRunningError:
            await self.update(
                project_workflow_id=project_workflow_id,
                schedule=schedule,
                paused=paused,
            )
        return schedule_id

    async def update(
        self,
        *,
        project_workflow_id: str,
        schedule: WorkflowSchedule,
        paused: bool,
    ) -> None:
        definition = self._definition(
            project_workflow_id=project_workflow_id,
            schedule=schedule,
            paused=paused,
        )
        try:
            await self._client.get_schedule_handle(self.schedule_id(project_workflow_id)).update(
                lambda _input: ScheduleUpdate(definition)
            )
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise
            await self._client.create_schedule(self.schedule_id(project_workflow_id), definition)

    async def pause(self, project_workflow_id: str) -> None:
        await self._client.get_schedule_handle(self.schedule_id(project_workflow_id)).pause(
            note="Paused from Tin"
        )

    async def remove_legacy_review_timeout(
        self, project_workflow_id: str, *, apply: bool = False
    ) -> bool:
        """Migrate a stored action without replacing its calendar, policy or pause state."""
        handle = self._client.get_schedule_handle(self.schedule_id(project_workflow_id))

        def updated(schedule: Schedule) -> ScheduleUpdate | None:
            action = schedule.action
            if (
                not isinstance(action, ScheduleActionStartWorkflow)
                or action.workflow != "tin.scheduled_dispatch"
                or action.id != f"tin-scheduled-dispatch:{project_workflow_id}"
            ):
                raise ValueError("Schedule is not the expected Tin dispatcher")
            if action.execution_timeout != timedelta(hours=24):
                if action.execution_timeout not in (None, timedelta(0)):
                    raise ValueError("Schedule has an unexpected execution timeout")
                return None
            # Retain raw payloads and SDK encoding metadata from describe().
            action = copy(action)
            action.execution_timeout = None
            migrated = copy(schedule)
            migrated.action = action
            return ScheduleUpdate(migrated)

        if not apply:
            return updated((await handle.describe()).schedule) is not None
        changed = False

        def update(current):
            nonlocal changed
            result = updated(current.description.schedule)
            changed = result is not None
            return result

        await handle.update(update)
        return changed

    async def resume(self, project_workflow_id: str) -> None:
        await self._client.get_schedule_handle(self.schedule_id(project_workflow_id)).unpause(
            note="Resumed from Tin"
        )

    async def delete(self, project_workflow_id: str) -> None:
        try:
            await self._client.get_schedule_handle(self.schedule_id(project_workflow_id)).delete()
        except RPCError as exc:
            if exc.status != RPCStatusCode.NOT_FOUND:
                raise

    @staticmethod
    def schedule_id(project_workflow_id: str) -> str:
        return f"tin-lite-project-workflow:{project_workflow_id}"

    def _definition(
        self,
        *,
        project_workflow_id: str,
        schedule: WorkflowSchedule,
        paused: bool,
    ) -> Schedule:
        hour, minute = (int(value) for value in schedule.local_time.split(":"))
        day_ranges = (
            [ScheduleRange((WEEKDAYS.index(day) + 1) % 7) for day in schedule.weekdays]
            if schedule.cadence == "weekly"
            else [ScheduleRange(0, 6, 1)]
        )
        return Schedule(
            action=ScheduleActionStartWorkflow(
                "tin.scheduled_dispatch",
                project_workflow_id,
                id=f"tin-scheduled-dispatch:{project_workflow_id}",
                task_queue=self._settings.task_queue,
                # Catch-up bounds late starts, not the lifetime of a reviewable child.
                # Keep the dispatcher alive until that child finishes (including review).
            ),
            spec=ScheduleSpec(
                calendars=[
                    ScheduleCalendarSpec(
                        minute=[ScheduleRange(minute)],
                        hour=[ScheduleRange(hour)],
                        day_of_week=day_ranges,
                    )
                ],
                time_zone_name=schedule.timezone,
                start_at=schedule.start_at,
                end_at=schedule.end_at,
            ),
            policy=SchedulePolicy(
                overlap=ScheduleOverlapPolicy.SKIP,
                catchup_window=timedelta(hours=24),
                pause_on_failure=False,
            ),
            state=ScheduleState(paused=paused),
        )
