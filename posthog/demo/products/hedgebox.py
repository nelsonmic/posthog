import datetime as dt
import math
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum, auto
from typing import Dict, List, Optional, Set, Tuple, cast

import pytz

from posthog.constants import INSIGHT_TRENDS, TRENDS_LINEAR, TRENDS_WORLD_MAP
from posthog.demo.matrix.matrix import Cluster, Matrix
from posthog.demo.matrix.models import SimPerson
from posthog.models import Cohort, Dashboard, DashboardTile, Experiment, FeatureFlag, Insight, InsightViewed

# This is a simulation of an online drive SaaS called Hedgebox
# See this RFC for the reasoning behind it:
# https://github.com/PostHog/product-internal/blob/main/requests-for-comments/2022-03-23-great-demo-data.md

# Simulation features:
# - the product is used by lots of personal users, but businesses bring the most revenue
# - most users are from the US, but there are blips all over the world
# - timezones are accurate on the country level
# - usage times are accurate taking into account time of day, timezone, and user profile (personal or business)
# - Hedgebox is sponsoring the well-known YouTube channel about technology Marius Tech Tips - there's a landing page
# - an experiment with a new signup page is running, and it's showing positive results
# - Internet Explorer users do worse

# See this flowchart for the layout of the product:
# https://www.figma.com/file/nmvylkFx4JdTRDqyo5Vkb5/Hedgebox-Paths

PRODUCT_NAME = "Hedgebox"

# URLs

SITE_URL = "https://hedgebox.net"

URL_HOME = f"{SITE_URL}/"
URL_MARIUS_TECH_TIPS = f"{SITE_URL}/mariustechtips/"
URL_PRICING = f"{SITE_URL}/pricing/"
URL_PRODUCT = f"{SITE_URL}/product/"

URL_SIGNUP = f"{SITE_URL}/signup/"
URL_LOGIN = f"{SITE_URL}/login/"
dyn_url_invite = lambda invite_id: f"{SITE_URL}/invite/{invite_id}/"

URL_FILES = f"{SITE_URL}/files/"
dyn_url_file = lambda file_id: f"{SITE_URL}/files/{file_id}/"

URL_ACCOUNT_SETTINGS = f"{SITE_URL}/account/settings/"
URL_ACCOUNT_BILLING = f"{SITE_URL}/account/billing/"
URL_ACCOUNT_TEAM = f"{SITE_URL}/account/team/"

# Event taxonomy

EVENT_SIGNED_UP = "signed_up"  # Properties: from_invite
EVENT_LOGGED_IN = "logged_in"  # No extra properties

EVENT_UPLOADED_FILE = "uploaded_file"  # Properties: file_type, file_size_b
EVENT_DOWNLOADED_FILE = "downloaded_file"  # Properties: file_type, file_size_b
EVENT_DELETED_FILE = "deleted_file"  # Properties: file_type, file_size_b
EVENT_SHARED_FILE_LINK = "shared_file_link"  # Properties: file_type, file_size_b

EVENT_UPGRADED_PLAN = "upgraded_plan"  # Properties: previous_plan, new_plan
EVENT_DOWNGRADED_PLAN = "downgraded_plan"  # Properties: previous_plan, new_plan

EVENT_INVITE_TEAM_MEMBER = "invited_team_member"  # No extra properties
EVENT_REMOVED_TEAM_MEMBER = "removed_team_member"  # No extra properties

EVENT_PAID_BILL = "paid_bill"  # Properties: used_mb, allowed_mb, plan, amount

# Group taxonomy

GROUP_TYPE_ACCOUNT = "account"  # Properties: name, used_mb, plan, team_size

# Feature flags

FILE_PREVIEWS_FLAG_KEY = "file-previews"
NEW_SIGNUP_PAGE_FLAG_KEY = "signup-page-4.0"
NEW_SIGNUP_PAGE_FLAG_ROLLOUT_PERCENT = 50
PROPERTY_NEW_SIGNUP_PAGE_FLAG = f"$feature/{NEW_SIGNUP_PAGE_FLAG_KEY}"
SIGNUP_SUCCESS_RATE_TEST = 0.5794
SIGNUP_SUCCESS_RATE_CONTROL = 0.4887

# World properties

# How many clusters should be companies (made up of business users) as opposed to social circles (personal users)
COMPANY_CLUSTERS_PROPORTION = 0.2


class HedgeboxReferrer(Enum):
    """Where a user knows about Hedgebox from."""

    WORD_OF_MOUTH = auto()
    COWORKER = auto()
    GOOGLE = auto()
    MARIUS_TECH_TIPS = auto()


class HedgeboxSessionIntent(Enum):
    """What the user has in mind for the current session."""

    CONSIDER_PRODUCT = auto()
    CHECK_MARIUS_TECH_TIPS_LINK = auto()
    UPLOAD_FILE = auto()
    DELETE_FILE = auto()
    SEE_OWN_FILE = auto()
    SHARE_FILE = auto()
    SEE_SHARED_FILE = auto()
    INVITE_TEAM_MEMBER = auto()
    REMOVE_TEAM_MEMBER = auto()
    JOIN_FROM_INVITE = auto()
    UPGRADE_PLAN = auto()
    DOWNGRADE_PLAN = auto()


class HedgeboxPlan(str, Enum):
    PERSONAL_FREE = "personal/free"
    PERSONAL_PRO = "personal/pro"
    BUSINESS_STANDARD = "business/standard"
    BUSINESS_ENTERPRISE = "business/enterprise"

    @property
    def is_business(self) -> bool:
        return self.startswith("business/")

    @property
    def higher_plan(self) -> Optional["HedgeboxPlan"]:
        if self == HedgeboxPlan.PERSONAL_FREE:
            return HedgeboxPlan.PERSONAL_PRO
        elif self == HedgeboxPlan.BUSINESS_STANDARD:
            return HedgeboxPlan.BUSINESS_ENTERPRISE
        else:
            return None

    @property
    def lower_plan(self) -> Optional["HedgeboxPlan"]:
        if self == HedgeboxPlan.PERSONAL_PRO:
            return HedgeboxPlan.PERSONAL_FREE
        elif self == HedgeboxPlan.BUSINESS_ENTERPRISE:
            return HedgeboxPlan.BUSINESS_STANDARD
        else:
            return None


@dataclass
class HedgeboxFile:
    id: str
    type: str
    size_b: int
    popularity: int


@dataclass
class HedgeboxAccount:
    id: str
    team_members: Set["HedgeboxPerson"]
    plan: HedgeboxPlan = field(default=HedgeboxPlan.PERSONAL_FREE)
    files: Dict[str, HedgeboxFile] = field(default_factory=dict)

    @property
    def current_allowed_mb(self) -> int:
        if self.plan == HedgeboxPlan.PERSONAL_FREE:
            return 10_000
        elif self.plan == HedgeboxPlan.PERSONAL_PRO:
            return 1_000_000
        elif self.plan == HedgeboxPlan.BUSINESS_STANDARD:
            return 5_000_000
        elif self.plan == HedgeboxPlan.BUSINESS_ENTERPRISE:
            return 100_000_000
        else:
            raise ValueError(f"Unknown plan: {self.plan}")

    @property
    def current_used_mb(self) -> int:
        return sum(file.size_b for file in self.files.values())

    @property
    def current_monthly_bill_usd(self) -> Decimal:
        if self.plan == HedgeboxPlan.PERSONAL_FREE:
            return Decimal("0.00")
        elif self.plan == HedgeboxPlan.PERSONAL_PRO:
            return Decimal("10.00")
        elif self.plan == HedgeboxPlan.BUSINESS_STANDARD:
            return Decimal("10.00") * len(self.team_members)
        elif self.plan == HedgeboxPlan.BUSINESS_ENTERPRISE:
            return Decimal("20.00") * len(self.team_members)
        else:
            raise ValueError(f"Unknown plan: {self.plan}")


class HedgeboxPerson(SimPerson):
    cluster: "HedgeboxCluster"

    # Constant properties
    person_id: str
    name: str
    email: str
    affinity: float  # 0 roughly means they won't like Hedgebox, 1 means they will

    # Internal state - plain
    _referrer: Optional[HedgeboxReferrer]
    _received_invite_id: Optional[str]
    _received_file_id: Optional[str]

    # Internal state - bounded
    _need: float  # 0 means no need, 1 means desperate
    _satisfaction: float  # -1 means hate, 0 means neutrality, 1 means love
    _personal_account: Optional[HedgeboxAccount]  # In company clusters the cluster-level account is used

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.person_id = self.cluster.random.randstr(False, 16)
        self.name = self.cluster.person_provider.full_name()
        self.email = self.cluster.person_provider.email()
        self.affinity = (
            self.cluster.random.betavariate(1.8, 1.2)
            if self._active_client.browser != "Internet Explorer"
            else self.cluster.random.betavariate(1, 1.4)
        )
        self._referrer = None
        self._received_invite_id = None
        self._received_file_id = None
        self._need = self.cluster.random.uniform(0.6 if self.kernel else 0, 1 if self.kernel else 0.2)
        self._satisfaction = 0.0
        self._personal_account = None
        while True:
            self.country_code = (
                "US" if self.cluster.random.random() < 0.9132 else self.cluster.address_provider.country_code()
            )
            try:  # Some tiny regions aren't in pytz - we want to omit those
                self.timezone = self.cluster.random.choice(pytz.country_timezones[self.country_code])
            except KeyError:
                continue
            else:
                break

    def __str__(self) -> str:
        return f"{self.name} <{self.email}>"

    def __hash__(self) -> int:
        return hash(self.email)

    # Internal state - derived to ensure bounds

    @property
    def need(self) -> float:
        return self._need

    @need.setter
    def need(self, value):
        self._need = max(0, min(1, value))

    @property
    def satisfaction(self) -> float:
        return self._satisfaction

    @satisfaction.setter
    def satisfaction(self, value):
        self._satisfaction = max(-1, min(1, value))

    @property
    def account(self) -> Optional[HedgeboxAccount]:
        return self.cluster._business_account if self.cluster.company_name else self._personal_account

    @account.setter
    def account(self, value):
        if self.cluster.company_name:
            self.cluster._business_account = value
        else:
            self._personal_account = value

    @property
    def has_signed_up(self) -> bool:
        return self.account is not None and self in self.account.team_members

    # Abstract methods

    def _fast_forward_to_next_session(self):
        while True:
            self._simulation_time += dt.timedelta(
                seconds=self.cluster.random.betavariate(2.5, 1 + self.need)
                * (36_000 if self.has_signed_up else 172_800)
                + 24
            )
            time_appropriateness: float
            # Check if it's night
            if 5 < self._simulation_time.hour < 23:
                time_appropriateness = 0.1
            # Check if it's 9 to 5 on a work day
            elif self._simulation_time.weekday() <= 5 and 9 <= self._simulation_time.hour <= 17:
                # Business users most likely to be active during the work day, personal users just the opposite
                time_appropriateness = 1 if self.cluster.company_name else 0.3
            else:
                time_appropriateness = 0.2 if self.cluster.company_name else 1

            if self.cluster.random.random() < time_appropriateness:
                return  # If the time is right, let's act - otherwise, let's advance further

    def _simulate_session(self):
        if (
            PROPERTY_NEW_SIGNUP_PAGE_FLAG not in self._super_properties
            and self._simulation_time >= self.cluster.matrix.new_signup_page_experiment_start
            and self._simulation_time < self.cluster.matrix.new_signup_page_experiment_end
        ):
            self._register(
                {
                    PROPERTY_NEW_SIGNUP_PAGE_FLAG: "test"
                    if self.cluster.random.random() < (NEW_SIGNUP_PAGE_FLAG_ROLLOUT_PERCENT / 100)
                    else "control"
                },
            )

        possible_intents_with_weights: List[Tuple[HedgeboxSessionIntent, float]]
        if self._received_invite_id:
            possible_intents_with_weights = [(HedgeboxSessionIntent.JOIN_FROM_INVITE, 1)]
        elif self._received_file_id:
            possible_intents_with_weights = [(HedgeboxSessionIntent.SEE_SHARED_FILE, 1)]
        elif not self.has_signed_up:
            possible_intents_with_weights = [
                (HedgeboxSessionIntent.CONSIDER_PRODUCT, 10),
                (HedgeboxSessionIntent.CHECK_MARIUS_TECH_TIPS_LINK, 1),
            ]
        else:
            account = cast(HedgeboxAccount, self.account)  # Must be set in this branch
            file_count = len(account.files)
            possible_intents_with_weights = [
                (HedgeboxSessionIntent.UPLOAD_FILE, 1),
                # The more files, the more likely to go to delete/download/share rather than upload
                (HedgeboxSessionIntent.DELETE_FILE, math.log10(file_count) / 8 if file_count else 0),
                (HedgeboxSessionIntent.SEE_OWN_FILE, math.log10(file_count + 1) if file_count else 0),
                (HedgeboxSessionIntent.SHARE_FILE, math.log10(file_count) / 3 if file_count else 0),
            ]
            if self.satisfaction > 0.5 and account.plan.higher_plan:
                possible_intents_with_weights.append((HedgeboxSessionIntent.UPGRADE_PLAN, 0.1))
            elif self.satisfaction < -0.5 and account.plan.lower_plan:
                possible_intents_with_weights.append((HedgeboxSessionIntent.DOWNGRADE_PLAN, 0.1))
            if account.plan.is_business and len(self.cluster.people) > 1:
                if len(account.team_members) < len(self.cluster.people):
                    possible_intents_with_weights.append((HedgeboxSessionIntent.INVITE_TEAM_MEMBER, 0.2))
                if len(account.team_members) > 1:
                    possible_intents_with_weights.append((HedgeboxSessionIntent.REMOVE_TEAM_MEMBER, 0.025))

        possible_intents, weights = zip(*possible_intents_with_weights)
        main_session_intent = self.cluster.random.choices(
            cast(Tuple[HedgeboxSessionIntent], possible_intents), cast(Tuple[float], weights)
        )[0]

        if main_session_intent == HedgeboxSessionIntent.CONSIDER_PRODUCT:
            self._consider_product()
        elif main_session_intent == HedgeboxSessionIntent.CHECK_MARIUS_TECH_TIPS_LINK:
            self._check_marius_tech_tips_link()
        elif main_session_intent == HedgeboxSessionIntent.UPLOAD_FILE:
            self._upload_file()
        elif main_session_intent == HedgeboxSessionIntent.DELETE_FILE:
            self._delete_file()
        elif main_session_intent == HedgeboxSessionIntent.SEE_OWN_FILE:
            self._see_own_file()
        elif main_session_intent == HedgeboxSessionIntent.SHARE_FILE:
            self._share_file()
        elif main_session_intent == HedgeboxSessionIntent.SEE_SHARED_FILE:
            self._see_shared_file()
        elif main_session_intent == HedgeboxSessionIntent.INVITE_TEAM_MEMBER:
            self._invite_team_member()
        elif main_session_intent == HedgeboxSessionIntent.REMOVE_TEAM_MEMBER:
            self._remove_team_member()
        elif main_session_intent == HedgeboxSessionIntent.UPGRADE_PLAN:
            self._upgrade_plan()
        elif main_session_intent == HedgeboxSessionIntent.DOWNGRADE_PLAN:
            self._downgrade_plan()
        else:
            raise ValueError(f"Unknown session intent: {main_session_intent}")

    # Page visits

    def _visit_home(self):
        self._capture_pageview(URL_HOME)
        self._advance_timer(1.8 + self.cluster.random.betavariate(1.5, 3) * 300)  # Viewing the page
        self.satisfaction += (self.cluster.random.betavariate(1.6, 1.2) - 0.5) * 0.1  # It's a somewhat nice page

    def _visit_marius_tech_tips(self):
        self._capture_pageview(URL_MARIUS_TECH_TIPS)
        self._advance_timer(1.2 + self.cluster.random.betavariate(1.5, 2) * 150)  # Viewing the page
        self.satisfaction += (self.cluster.random.betavariate(1.6, 1.2) - 0.5) * 0.4  # The user may be in target or not

    def _visit_pricing(self):
        self._capture_pageview(URL_PRICING)
        self._advance_timer(1.2 + self.cluster.random.betavariate(1.5, 2) * 200)  # Viewing the page

    def _visit_product(self):
        self._capture_pageview(URL_PRODUCT)
        self._advance_timer(1.2 + self.cluster.random.betavariate(1.5, 2) * 200)  # Viewing the page

    def _visit_sign_up(self):
        if self.cluster.company_name and not self.kernel:
            raise ValueError("Only the kernel can sign up in a company cluster")

        self._capture_pageview(URL_SIGNUP)  # Visiting the sign-up page

        if self.has_signed_up:  # Signed up already!
            self._advance_timer(5 + self.cluster.random.betavariate(2, 1.3) * 19)
            return self._visit_login()

        # Signup is faster with the new signup page
        is_on_new_signup_page = self._super_properties.get(PROPERTY_NEW_SIGNUP_PAGE_FLAG) == "test"
        success_rate = SIGNUP_SUCCESS_RATE_TEST if is_on_new_signup_page else SIGNUP_SUCCESS_RATE_CONTROL
        # What's the outlook?
        success = self.cluster.random.random() < success_rate
        self._advance_timer(
            9 + self.cluster.random.betavariate(1.2, 2) * (60 if not success else 120 if is_on_new_signup_page else 170)
        )  # Looking at things, filling out forms
        # More likely to finish signing up with the new signup page
        if success:  # Let's do this!
            self.account = HedgeboxAccount(id=str(self.roll_uuidt()), team_members={self})  # TODO: Add billing
            self._capture(EVENT_SIGNED_UP, {"from_invite": False})
            self._advance_timer(self.cluster.random.uniform(0.1, 0.2))
            self._identify(self.person_id, {"email": self.email, "name": self.name})
            self._group(
                GROUP_TYPE_ACCOUNT,
                self.account.id,
                {
                    "name": self.cluster.company_name or self.name,
                    "used_mb": 0,
                    "plan": self.account.plan,
                    "team_size": 1,
                },
            )
            self.satisfaction += (self.cluster.random.betavariate(1.5, 1.2) - 0.5) * 0.2
        else:  # Something didn't go right...
            self.satisfaction += (self.cluster.random.betavariate(1, 3) - 0.75) * 0.5

    def _visit_login(self):
        if self.cluster.company_name and not self.kernel:
            raise ValueError("Only the kernel can sign up in a company cluster")

        self._capture_pageview(URL_LOGIN)

        if not self.has_signed_up:  # Not signed up yet!
            self._advance_timer(3 + self.cluster.random.betavariate(1.4, 1.2) * 14)
            return self._visit_sign_up()

        success = self.cluster.random.random() < 0.95  # There's always a tiny chance the user will resign
        self._advance_timer(2 + self.cluster.random.betavariate(1.2, 1.2) * (29 if success else 17))

        if success:
            self._capture(EVENT_LOGGED_IN)
            self._advance_timer(self.cluster.random.uniform(0.1, 0.2))
            self._identify(self.person_id)

    def _visit_invite(self, invite_id: str):
        pass  # TODO

    def _visit_files(self):
        pass  # TODO

    def _visit_own_file(self, file_id: str):
        pass  # TODO

    def _visit_shared_file(self, file_id: str):
        self._capture_pageview(dyn_url_file(file_id))
        # TODO

    def _visit_account_settings(self):
        pass  # TODO

    def _visit_account_billing(self):
        pass  # TODO

    def _visit_account_team(self):
        pass  # TODO

    # Intent flows

    def _consider_product(self):
        pass  # TODO

    def _check_marius_tech_tips_link(self):
        pass  # TODO

    def _upload_file(self):
        self._advance_timer(self.cluster.random.betavariate(2.5, 1.1) * 95)
        file_count = self.cluster.random.randint(1, 13)
        for _ in range(file_count):
            self._capture(
                EVENT_UPLOADED_FILE, properties={"file_extension": self.cluster.file_provider.extension(),},
            )
        self.satisfaction += self.cluster.random.uniform(-0.19, 0.2)
        if self.satisfaction > 0.9:
            self._affect_neighbors(lambda other: other._move_needle("need", 0.05))

    def _delete_file(self):
        pass  # TODO

    def _see_own_file(self):
        pass  # TODO

    def _share_file(self):
        self._capture(EVENT_SHARED_FILE_LINK)  # TODO

    def _see_shared_file(self):
        pass  # TODO

    def _invite_team_member(self):
        pass  # TODO

    def _remove_team_member(self):
        pass  # TODO

    def _upgrade_plan(self):
        pass  # TODO

    def _downgrade_plan(self):
        pass  # TODO


class HedgeboxCluster(Cluster):
    matrix: "HedgeboxMatrix"

    MIN_RADIUS: int = 0
    MAX_RADIUS: int = 6

    # Properties
    company_name: Optional[str]  # None means the cluster is a social circle instead of a company

    # Internal state - plain
    _business_account: Optional[HedgeboxAccount]  # In social circle clusters the person-level account is used

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        is_company = self.random.random() < COMPANY_CLUSTERS_PROPORTION
        self.company_name = self.finance_provider.company() if is_company else None
        self._business_account = None

    def __str__(self) -> str:
        return self.company_name or f"Social Circle #{self.index+1}"

    def _radius_distribution(self) -> int:
        return int(self.MIN_RADIUS + self.random.betavariate(1.5, 5) * (self.MAX_RADIUS - self.MIN_RADIUS))

    def _initation_distribution(self) -> float:
        return self.random.betavariate(1.8, 1)


class HedgeboxMatrix(Matrix):
    person_model = HedgeboxPerson
    cluster_model = HedgeboxCluster

    new_signup_page_experiment_start: dt.datetime
    new_signup_page_experiment_end: dt.datetime

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Start new signup page experiment roughly halfway through the simulation, end soon before `now`
        self.new_signup_page_experiment_end = self.now - dt.timedelta(days=2, hours=3, seconds=43)
        self.new_signup_page_experiment_start = self.start + (self.new_signup_page_experiment_end - self.start) / 2

    def set_project_up(self, team, user):
        super().set_project_up(team, user)
        team.name = PRODUCT_NAME

        # Dashboard: Key metrics (project home)
        key_metrics_dashboard = Dashboard.objects.create(
            team=team, name="🔑 Key metrics", description="Company overview.", pinned=True
        )
        team.primary_dashboard = key_metrics_dashboard
        weekly_signups_insight = Insight.objects.create(
            team=team,
            dashboard=key_metrics_dashboard,
            saved=True,
            name="Weekly signups",
            filters={
                "events": [{"id": EVENT_SIGNED_UP, "type": "events", "order": 0}],
                "actions": [],
                "display": TRENDS_LINEAR,
                "insight": INSIGHT_TRENDS,
                "interval": "week",
                "date_from": "-1m",
            },
            last_modified_at=self.now - dt.timedelta(days=23),
            last_modified_by=user,
        )
        DashboardTile.objects.create(
            dashboard=key_metrics_dashboard,
            insight=weekly_signups_insight,
            color="blue",
            layouts={
                "sm": {"h": 5, "w": 6, "x": 0, "y": 0, "minH": 5, "minW": 3},
                "xs": {"h": 5, "w": 1, "x": 0, "y": 0, "minH": 5, "minW": 3, "moved": False, "static": False},
            },
        )
        signups_by_country_insight = Insight.objects.create(
            team=team,
            dashboard=key_metrics_dashboard,
            saved=True,
            name="Last month's signups by country",
            filters={
                "events": [{"id": EVENT_SIGNED_UP, "type": "events", "order": 0}],
                "actions": [],
                "display": TRENDS_WORLD_MAP,
                "insight": INSIGHT_TRENDS,
                "breakdown_type": "event",
                "breakdown": "$geoip_country_code",
                "date_from": "-1m",
            },
            last_modified_at=self.now - dt.timedelta(days=6),
            last_modified_by=user,
        )
        DashboardTile.objects.create(
            dashboard=key_metrics_dashboard,
            insight=signups_by_country_insight,
            layouts={
                "sm": {"h": 5, "w": 6, "x": 6, "y": 0, "minH": 5, "minW": 3},
                "xs": {"h": 5, "w": 1, "x": 0, "y": 5, "minH": 5, "minW": 3, "moved": False, "static": False},
            },
        )
        signup_from_homepage_funnel = Insight.objects.create(
            team=team,
            dashboard=key_metrics_dashboard,
            saved=True,
            name="Homepage view to signup conversion",
            filters={
                "events": [
                    {
                        "custom_name": "Viewed homepage",
                        "id": "$pageview",
                        "name": "$pageview",
                        "type": "events",
                        "order": 0,
                        "properties": [
                            {
                                "key": "$current_url",
                                "type": "event",
                                "value": "https://hedgebox.net/",
                                "operator": "exact",
                            }
                        ],
                    },
                    {
                        "custom_name": "Viewed signup page",
                        "id": "$pageview",
                        "name": "$pageview",
                        "type": "events",
                        "order": 1,
                        "properties": [
                            {
                                "key": "$current_url",
                                "type": "event",
                                "value": "https:\\/\\/hedgebox\\.net\\/register($|\\/)",
                                "operator": "regex",
                            }
                        ],
                    },
                    {"custom_name": "Signed up", "id": "signed_up", "name": "signed_up", "type": "events", "order": 2},
                ],
                "actions": [],
                "display": "FunnelViz",
                "insight": "FUNNELS",
                "interval": "day",
                "funnel_viz_type": "steps",
                "filter_test_accounts": True,
                "date_from": "-1m",
            },
            last_modified_at=self.now - dt.timedelta(days=19),
            last_modified_by=user,
        )
        DashboardTile.objects.create(
            dashboard=key_metrics_dashboard,
            insight=signup_from_homepage_funnel,
            layouts={
                "sm": {"h": 5, "w": 6, "x": 0, "y": 5, "minH": 5, "minW": 3},
                "xs": {"h": 5, "w": 1, "x": 0, "y": 10, "minH": 5, "minW": 3, "moved": False, "static": False},
            },
        )
        weekly_uploader_retention = Insight.objects.create(
            team=team,
            dashboard=key_metrics_dashboard,
            saved=True,
            name="Weekly uploader retention",
            filters={
                "period": "Week",
                "display": "ActionsTable",
                "insight": "RETENTION",
                "properties": [],
                "target_entity": {"id": "uploaded_file", "name": "uploaded_file", "type": "events", "order": 0},
                "retention_type": "retention_first_time",
                "total_intervals": 11,
                "returning_entity": {"id": "uploaded_file", "name": "uploaded_file", "type": "events", "order": 0},
                "filter_test_accounts": True,
            },
            last_modified_at=self.now - dt.timedelta(days=34),
            last_modified_by=user,
        )
        DashboardTile.objects.create(
            dashboard=key_metrics_dashboard,
            insight=weekly_uploader_retention,
            layouts={
                "sm": {"h": 5, "w": 6, "x": 6, "y": 5, "minH": 5, "minW": 3},
                "xs": {"h": 5, "w": 1, "x": 0, "y": 15, "minH": 5, "minW": 3, "moved": False, "static": False},
            },
        )

        # InsightViewed
        InsightViewed.objects.bulk_create(
            (
                InsightViewed(
                    team=team,
                    user=user,
                    insight=insight,
                    last_viewed_at=(
                        self.now - dt.timedelta(days=self.random.randint(0, 3), minutes=self.random.randint(5, 60))
                    ),
                )
                for insight in Insight.objects.filter(team=team)
            )
        )
        # Cohorts
        Cohort.objects.create(
            team=team,
            name="Signed-up users",
            created_by=user,
            groups=[{"properties": [{"key": "email", "type": "person", "value": "is_set", "operator": "is_set"}]}],
        )
        real_users_cohort = Cohort.objects.create(
            team=team,
            name="Real users",
            description="People who don't belong to the Hedgebox team.",
            created_by=user,
            groups=[
                {"properties": [{"key": "email", "type": "person", "value": "@hedgebox.net$", "operator": "not_regex"}]}
            ],
        )
        team.test_account_filters = [{"key": "id", "type": "cohort", "value": real_users_cohort.pk}]

        # Feature flags
        new_signup_page_flag = FeatureFlag.objects.create(
            team=team,
            key=FILE_PREVIEWS_FLAG_KEY,
            name="File previews (ticket #2137). Work-in-progress, so only visible internally at the moment",
            filters={
                "groups": [
                    {
                        "properties": [
                            {
                                "key": "email",
                                "type": "person",
                                "value": [
                                    "mark.s@hedgebox.net",
                                    "helly.r@hedgebox.net",
                                    "irving.b@hedgebox.net",
                                    "dylan.g@hedgebox.net",
                                ],
                                "operator": "exact",
                            }
                        ]
                    }
                ]
            },
            created_by=user,
            created_at=self.now - dt.timedelta(days=15),
        )

        # Experiments
        new_signup_page_flag = FeatureFlag.objects.create(
            team=team,
            key=NEW_SIGNUP_PAGE_FLAG_KEY,
            name="New sign-up flow",
            filters={
                "groups": [{"properties": [], "rollout_percentage": None}],
                "multivariate": {
                    "variants": [
                        {"key": "control", "rollout_percentage": 100 - NEW_SIGNUP_PAGE_FLAG_ROLLOUT_PERCENT},
                        {"key": "test", "rollout_percentage": NEW_SIGNUP_PAGE_FLAG_ROLLOUT_PERCENT},
                    ]
                },
            },
            created_by=user,
            created_at=self.new_signup_page_experiment_start - dt.timedelta(hours=1),
        )
        Experiment.objects.create(
            team=team,
            name="New sign-up flow",
            description="We've rebuilt our sign-up page to offer a more personalized experience. Let's see if this version performs better with potential users.",
            feature_flag=new_signup_page_flag,
            created_by=user,
            filters={
                "events": [
                    {
                        "id": "$pageview",
                        "name": "$pageview",
                        "type": "events",
                        "order": 0,
                        "properties": [
                            {
                                "key": "$current_url",
                                "type": "event",
                                "value": "https:\\/\\/hedgebox\\.net\\/register($|\\/)",
                                "operator": "regex",
                            }
                        ],
                    },
                    {"id": "signed_up", "name": "signed_up", "type": "events", "order": 1},
                ],
                "actions": [],
                "display": "FunnelViz",
                "insight": "FUNNELS",
                "interval": "day",
                "funnel_viz_type": "steps",
                "filter_test_accounts": True,
            },
            parameters={
                "feature_flag_variants": [
                    {"key": "control", "rollout_percentage": 100 - NEW_SIGNUP_PAGE_FLAG_ROLLOUT_PERCENT},
                    {"key": "test", "rollout_percentage": NEW_SIGNUP_PAGE_FLAG_ROLLOUT_PERCENT},
                ],
                "recommended_sample_size": int(len(self.clusters) * 0.43),
                "recommended_running_time": None,
                "minimum_detectable_effect": 1,
            },
            start_date=self.new_signup_page_experiment_start,
            end_date=self.new_signup_page_experiment_end,
            created_at=new_signup_page_flag.created_at,
        )
