import datetime as dt
from abc import ABC, abstractmethod
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from itertools import chain
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    DefaultDict,
    Deque,
    Dict,
    Iterable,
    List,
    Literal,
    Optional,
    Set,
    Tuple,
    TypeVar,
)
from urllib.parse import urlparse
from uuid import UUID

import pytz

from posthog.models.utils import UUIDT

if TYPE_CHECKING:
    from posthog.demo.matrix.matrix import Cluster

# Event name constants to be used in simulations
EVENT_PAGEVIEW = "$pageview"
EVENT_PAGELEAVE = "$pageleave"
EVENT_AUTOCAPTURE = "$autocapture"
EVENT_IDENTIFY = "$identify"
EVENT_GROUP_IDENTIFY = "$groupidentify"

PROPERTY_GEOIP_COUNTRY_CODE = "$geoip_country_code"

# Properties who get `$set_once` implicitly as `$initial_foo` - source of truth in plugin-server/src/utils/db/utils.ts
PROPERTIES_WITH_IMPLICIT_INITIAL_VALUE_TRACKING = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "gclid",
    "fbclid",
    "$browser",
    "$browser_version",
    "$device_type",
    "$current_url",
    "$pathname",
    "$os",
    "$referring_domain",
    "$referrer",
}

Properties = Dict[str, Any]
SP = TypeVar("SP", bound="SimPerson")
Effect = Callable[[SP], Any]


class SimSessionIntent(Enum):
    """An enumeration of session intents.

    An intent is determined for each session a user starts, and it informs their behavior during that session."""

    pass


@dataclass
class SimEvent:
    """A simulated event."""

    event: str
    distinct_id: str
    properties: Properties
    timestamp: dt.datetime

    def __str__(self) -> str:
        separator = (
            "-" if self.timestamp < dt.datetime.now(dt.timezone.utc) else "+"
        )  # Future events are denoted by a '+'
        display = f"{self.timestamp} {separator} {self.event} # {self.properties['$distinct_id']}"
        if current_url := self.properties.get("$current_url"):
            display += f" @ {current_url}"
        return display


class SimBrowserClient:
    """A browser client, which enables client-side tracking simulation."""

    # Parent
    person: "SimPerson"

    # Properties
    device_id: str
    device_type: str
    os: str
    browser: str

    # State
    active_distinct_id: str
    active_session_id: Optional[str]
    super_properties: Properties
    current_url: Optional[str]
    is_logged_in: bool

    def __init__(self, person: "SimPerson"):
        self.person = person
        self.device_type, self.os, self.browser = person.cluster.properties_provider.device_type_os_browser()
        self.device_id = str(UUID(int=person.cluster.random.getrandbits(128)))
        self.active_distinct_id = self.device_id  # Pre-`$identify`, the device ID is used as the distinct ID
        self.active_session_id = None
        self.super_properties = {}
        self.current_url = None
        self.is_logged_in = True

    def __enter__(self):
        """Start session within client."""
        self.active_session_id = str(self.person.roll_uuidt())

    def __exit__(self, exc_type, exc_value, exc_traceback):
        """End session within client. Handles `$pageleave` event."""
        if self.current_url is not None:
            self.capture(EVENT_PAGELEAVE)
            self.current_url = None

    def capture(self, event: str, properties: Optional[Properties] = None):
        """Capture an arbitrary event. Similar to JS `posthog.capture()`."""
        combined_properties: Properties = {
            "$distinct_id": self.active_distinct_id,
            "$lib": "web",
            "$device_type": self.device_type,
            "$os": self.os,
            "$browser": self.browser,
            "$session_id": self.active_session_id,
            "$device_id": self.device_id,
            "$groups": self.person._groups.copy(),
            "$timestamp": self.person.simulation_time.isoformat(),
            "$time": self.person.simulation_time.timestamp(),
        }
        if self.super_properties:
            combined_properties.update(self.super_properties)
        if self.current_url is not None:
            parsed_current_url = urlparse(self.current_url)
            combined_properties["$current_url"] = self.current_url
            combined_properties["$host"] = parsed_current_url.netloc
            combined_properties["$pathname"] = parsed_current_url.path
        if "$set" not in combined_properties:
            combined_properties["$set"] = {}
        if properties:
            if referrer := properties.get("$referrer"):
                referring_domain = urlparse(referrer).netloc if referrer != "$direct" else referrer
                referrer_properties = {"$referrer": referrer, "$referring_domain": referring_domain}
                self.register(referrer_properties)
                combined_properties["$set"].update(referrer_properties)
                combined_properties["$referring_domain"] = referring_domain
            combined_properties.update(properties)
        # GeoIP
        combined_properties[PROPERTY_GEOIP_COUNTRY_CODE] = self.person.country_code
        combined_properties["$set"][PROPERTY_GEOIP_COUNTRY_CODE] = self.person.country_code
        if feature_flags := self.person.decide_feature_flags():
            for flag_key, flag_value in feature_flags.items():
                combined_properties[f"$feature/{flag_key}"] = flag_value
        # Saving
        self.person._append_event(event, distinct_id=self.active_distinct_id, properties=combined_properties or {})

    def capture_pageview(
        self, current_url: str, properties: Optional[Properties] = None, *, referrer: Optional[str] = None
    ):
        """Capture a $pageview event. $pageleave is handled implicitly."""
        if self.current_url is not None:
            self.capture(EVENT_PAGELEAVE)
        self.person.advance_timer(self.person.cluster.random.uniform(0.02, 0.1))  # A page doesn't load instantly
        self.current_url = current_url
        self.capture(EVENT_PAGEVIEW, properties)

    def identify(self, distinct_id: Optional[str], set_properties: Optional[Properties] = None):
        """Identify person in active client. Similar to JS `posthog.identify()`.

        Use with distinct_id=None for `posthog.people.set()`-like behavior."""
        if set_properties is None:
            set_properties = {}
        identify_properties = {"$distinct_id": self.active_distinct_id, "$set": set_properties}
        if distinct_id:
            self.is_logged_in = True
            if self.device_id == self.active_distinct_id:
                identify_properties["$anon_distinct_id"] = self.device_id
            identify_properties["$user_id"] = distinct_id
            self.active_distinct_id = distinct_id
        self.capture(EVENT_IDENTIFY, identify_properties)

    def reset(self):
        """Reset active client, for instance when the user logs out. Similar to JS `posthog.reset()`."""
        self.active_distinct_id = self.device_id
        self.is_logged_in = True

    def register(self, super_properties: Properties):
        """Register super properties. Similar to JS `posthog.register()`."""
        self.super_properties.update(super_properties)

    def unregister(self, *super_property_keys: str):
        """Removes super properties. Similar to JS `posthog.unregister()`."""
        for key in super_property_keys:
            self.super_properties.pop(key)

    def group(self, group_type: str, group_key: str, set_properties: Optional[Properties] = None):
        """Link the person to the specified group. Similar to JS `posthog.group()`."""
        if set_properties is None:
            set_properties = {}
        self.person._groups[group_type] = group_key
        self.person.cluster.matrix._update_group(group_type, group_key, set_properties)
        self.capture(
            EVENT_GROUP_IDENTIFY, {"$group_type": group_type, "$group_key": group_key, "$group_set": set_properties}
        )


class SimPerson(ABC):
    """A simulation agent, representing an individual person."""

    # Cluster-level metadata
    cluster: "Cluster"
    kernel: bool  # Whether this person is the cluster kernel. Kernels are the most likely to become users
    x: int
    y: int

    # Constant properties
    country_code: str
    timezone: str

    # Exposed state - present
    past_events: List[SimEvent]
    future_events: List[SimEvent]
    scheduled_effects: Deque[Tuple[dt.datetime, Effect]]

    # Exposed state - at `now`
    distinct_ids_at_now: Set[str]
    properties_at_now: Properties

    # Internal state
    finito: bool  # Whether this person has been simulated to completion
    active_client: SimBrowserClient  # Client being used by person
    all_time_pageview_counts: DefaultDict[str, int]  # Pageview count per URL across all time
    session_pageview_counts: DefaultDict[str, int]  # Pageview count per URL across the ongoing session
    active_session_intent: SimSessionIntent
    _simulation_time: dt.datetime  # Current simulation time, populated by running .simulate()
    _groups: Dict[str, str]
    _distinct_ids: Set[str]
    _properties: Properties

    @abstractmethod
    def __init__(self, *, kernel: bool, cluster: "Cluster", x: int, y: int):
        self.past_events = []
        self.future_events = []
        self.scheduled_effects = Deque()
        self.kernel = kernel
        self.cluster = cluster
        self.x = x
        self.y = y
        self.finito = False
        self.active_client = SimBrowserClient(self)
        self.all_time_pageview_counts = defaultdict(int)
        self.session_pageview_counts = defaultdict(int)
        self._groups = {}
        self._distinct_ids = set()
        self._properties = {}

    def __str__(self) -> str:
        """Return person ID. Overriding this is recommended but optional."""
        return " / ".join(self._distinct_ids) if self._distinct_ids else "???"

    # Helpers

    @property
    def all_events(self) -> Iterable[SimEvent]:
        return chain(self.past_events, self.future_events)

    @property
    def first_event(self) -> Optional[SimEvent]:
        return self.past_events[0] if self.past_events else (self.future_events[0] if self.future_events else None)

    @property
    def last_event(self) -> Optional[SimEvent]:
        return self.future_events[-1] if self.future_events else (self.past_events[-1] if self.past_events else None)

    # Public methods

    def simulate(self):
        """Synchronously simulate this person's behavior for the whole duration of the simulation."""
        if hasattr(self, "simulation_time"):
            raise Exception(f"Person {self} already has been simulated")
        self.simulation_time = self.cluster.start.astimezone(pytz.timezone(self.timezone))
        self._distinct_ids.add(self.active_client.device_id)
        while self.simulation_time <= self.cluster.end:
            next_session_datetime = self.determine_next_session_datetime()
            self._fast_forward(next_session_datetime)
            if new_session_intent := self.determine_session_intent():
                self.active_session_intent = new_session_intent
            else:
                continue  # If there's no intent, let's skip ahead
            self.session_pageview_counts.clear()
            with self.active_client:
                self.simulate_session()
        self.finito = True

    def schedule_effect(self, timestamp: dt.datetime, effect: Effect):
        """Schedule an effect to apply at a given time.

        An effect is a function that runs on the person, so it can change the person's state."""
        self.scheduled_effects.append((timestamp, effect))

    # Abstract methods

    def decide_feature_flags(self) -> Dict[str, Any]:
        """Determine feature flags in force at present."""
        return {}

    @abstractmethod
    def determine_next_session_datetime(self) -> dt.datetime:
        """Intelligently advance timer to the time of the next session."""
        raise NotImplementedError()

    @abstractmethod
    def determine_session_intent(self) -> Optional[SimSessionIntent]:
        """Determine the session intent for the session that's about to start."""
        raise NotImplementedError()

    @abstractmethod
    def simulate_session(self):
        """Simulate a single session based on current agent state. This is how subclasses can define user behavior."""
        raise NotImplementedError()

    # Neighbor state

    def affect_neighbors(self, effect: Effect):
        """Schedule the provided effect lambda for all neighbors.

        Because agents are simulated synchronously, the effect will only apply to neighbors who haven't been
        simulated yet, but that's OK - the results are interesting this way too.
        """
        for neighbor in self.cluster._list_amenable_neighbors(self.x, self.y):
            neighbor.schedule_effect(self.simulation_time, effect)

    # Person state

    @property
    def simulation_time(self) -> dt.datetime:
        return self._simulation_time

    @simulation_time.setter
    def simulation_time(self, value: dt.datetime):
        self._simulation_time = value
        if not hasattr(self, "distinct_ids_at_now") and self.simulation_time >= self.cluster.now:
            # If we've just reached matrix's `now`, take a snapshot of the current state
            # for dividing past and future events
            self.distinct_ids_at_now = self._distinct_ids.copy()
            self.properties_at_now = deepcopy(self._properties)

    def advance_timer(self, seconds: float):
        """Advance simulation time by the given amount of time."""
        self.simulation_time += dt.timedelta(seconds=seconds)

    def set_attribute(self, attr: str, value: Any) -> Literal[True]:
        """Set the person's attribute.

        Useful for defining effects,which are lambdas. Chain them with `and`."""
        setattr(self, attr, value)
        return True

    def move_attribute(self, attr: str, delta: float) -> Literal[True]:
        """Move the person's attribute by the given delta.

        Useful for defining effects, which are lambdas. Chain them with `and`."""
        setattr(self, attr, getattr(self, attr) + delta)
        return True

    def _fast_forward(self, next_session_datetime: dt.datetime):
        """Apply all effects that are due at the current time."""
        while True:
            if not self.scheduled_effects or self.scheduled_effects[0][0] > next_session_datetime:
                break
            effect_datetime, effect_lambda = self.scheduled_effects.popleft()
            if self.simulation_time < effect_datetime:
                self.simulation_time = effect_datetime
            effect_lambda(self)
        if self.simulation_time < next_session_datetime:
            self.simulation_time = next_session_datetime

    def _append_event(self, event: str, *, distinct_id: str, properties: Properties):
        """Append event to `past_events` or `future_events`, whichever is appropriate."""
        sim_event = SimEvent(
            event=event, distinct_id=distinct_id, properties=properties, timestamp=self.simulation_time
        )
        appropriate_events = self.future_events if sim_event.timestamp > self.cluster.now else self.past_events
        appropriate_events.append(sim_event)
        self._distinct_ids.add(distinct_id)
        if event == EVENT_PAGEVIEW:
            self.all_time_pageview_counts[properties["$current_url"]] += 1
            self.session_pageview_counts[properties["$current_url"]] += 1
        # $set/$set_once processing
        set_properties = properties.get("$set")
        set_once_properties = properties.get("$set_once", {})
        if set_properties:
            for key, value in set_properties.items():
                if key in PROPERTIES_WITH_IMPLICIT_INITIAL_VALUE_TRACKING:
                    set_once_properties[f"$initial_{key.replace('$', '')}"] = value
        if set_once_properties:
            for key, value in set_once_properties.items():
                if key not in self._properties:
                    self._properties[key] = value
        if set_properties:
            self._properties.update(set_properties)

    # Utilities

    def roll_uuidt(self, at_timestamp: Optional[dt.datetime] = None) -> UUIDT:
        if at_timestamp is None:
            at_timestamp = self.simulation_time
        return UUIDT(int(at_timestamp.timestamp() * 1000), seeded_random=self.cluster.random)
