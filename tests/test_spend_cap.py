"""Tests for aws/spend_cap_function.py using in-memory fakes. Run: python tests/test_spend_cap.py"""
import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "aws"))
import spend_cap_function as sc  # noqa: E402


class NotFound(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}


class Sched:
    def __init__(self, items):
        self.items = items  # name -> dict
    def list_schedules(self, **kw):
        return {"Schedules": [{"Name": n, "GroupName": "default", "State": v["State"]} for n, v in self.items.items()]}
    def get_schedule(self, GroupName, Name):
        if Name not in self.items:
            raise NotFound("ResourceNotFoundException")
        return dict(copy.deepcopy(self.items[Name]), Name=Name, GroupName=GroupName)
    def update_schedule(self, **kw):
        for req in ("Name", "ScheduleExpression", "FlexibleTimeWindow", "Target", "State"):
            assert req in kw, req
        stored = {k: v for k, v in kw.items() if k not in ("Name", "GroupName")}
        self.items[kw["Name"]] = stored


class Lam:
    def __init__(self, conc):
        self.conc = conc  # name -> reserved or None
    def list_functions(self, **kw):
        return {"Functions": [{"FunctionName": n} for n in self.conc]}
    def get_function_concurrency(self, FunctionName):
        v = self.conc[FunctionName]
        return {} if v is None else {"ReservedConcurrentExecutions": v}
    def put_function_concurrency(self, FunctionName, ReservedConcurrentExecutions):
        self.conc[FunctionName] = ReservedConcurrentExecutions
    def delete_function_concurrency(self, FunctionName):
        self.conc[FunctionName] = None


class S3:
    def __init__(self, pab):
        self.pab = pab  # bucket -> config or None
    def list_buckets(self):
        return {"Buckets": [{"Name": n} for n in self.pab]}
    def get_public_access_block(self, Bucket):
        if self.pab[Bucket] is None:
            raise NotFound("NoSuchPublicAccessBlockConfiguration")
        return {"PublicAccessBlockConfiguration": dict(self.pab[Bucket])}
    def put_public_access_block(self, Bucket, PublicAccessBlockConfiguration):
        self.pab[Bucket] = dict(PublicAccessBlockConfiguration)
    def delete_public_access_block(self, Bucket):
        self.pab[Bucket] = None


class SSM:
    def __init__(self):
        self.p = {}
    def get_parameter(self, Name):
        if Name not in self.p:
            raise NotFound("ParameterNotFound")
        return {"Parameter": {"Value": self.p[Name]}}
    def put_parameter(self, Name, Value, Type, Overwrite):
        self.p[Name] = Value
    def delete_parameter(self, Name):
        del self.p[Name]


class SNS:
    def __init__(self):
        self.sent = []
    def publish(self, **kw):
        self.sent.append(kw)


class Ctx:
    function_name = "spend-cap-killswitch"


OPEN = {"BlockPublicAcls": False, "IgnorePublicAcls": False, "BlockPublicPolicy": False, "RestrictPublicBuckets": False}


def world():
    sched = Sched({
        "every-minute": {"State": "ENABLED", "ScheduleExpression": "rate(1 minute)", "ScheduleExpressionTimezone": "America/Los_Angeles",
                         "FlexibleTimeWindow": {"Mode": "OFF"}, "Target": {"Arn": "a", "RoleArn": "r", "Input": "{}"}},
        "one-time": {"State": "ENABLED", "ScheduleExpression": "at(2026-11-24T14:00:00)", "ActionAfterCompletion": "DELETE",
                     "FlexibleTimeWindow": {"Mode": "OFF"}, "Target": {"Arn": "a", "RoleArn": "r"}},
        "already-off": {"State": "DISABLED", "ScheduleExpression": "rate(1 day)", "FlexibleTimeWindow": {"Mode": "OFF"}, "Target": {"Arn": "a", "RoleArn": "r"}},
    })
    lam = Lam({"spotify-sync": None, "auth": 5, "already-zero": 0, "spend-cap-killswitch": None})
    s3 = S3({"public": dict(OPEN), "none": None, "private": dict(sc.ALL_BLOCKED)})
    return {"scheduler": sched, "lambda": lam, "s3": s3, "ssm": SSM(), "sns": SNS()}


def snapshot(c):
    return copy.deepcopy((c["scheduler"].items, c["lambda"].conc, c["s3"].pab))


def test_dry_run_changes_nothing():
    c = world()
    before = snapshot(c)
    r = sc.handler({"action": "freeze", "dry_run": True}, Ctx(), clients=c)
    assert snapshot(c) == before
    assert sorted(r["schedules"]) == ["default/every-minute", "default/one-time"]
    assert sorted(r["lambdas"]) == ["auth", "spotify-sync"]
    assert sorted(r["buckets"]) == ["none", "public"]
    assert c["ssm"].p == {} and c["sns"].sent == []


def test_freeze_then_restore_round_trips():
    c = world()
    before = snapshot(c)
    sc.handler({"Records": [{"Sns": {"Message": "budget"}}]}, Ctx(), clients=c)
    sched, conc, pab = c["scheduler"].items, c["lambda"].conc, c["s3"].pab
    assert all(v["State"] == "DISABLED" for v in sched.values())
    assert sched["one-time"]["ActionAfterCompletion"] == "DELETE"
    assert sched["every-minute"]["ScheduleExpressionTimezone"] == "America/Los_Angeles"
    assert conc == {"spotify-sync": 0, "auth": 0, "already-zero": 0, "spend-cap-killswitch": None}
    assert pab["public"] == sc.ALL_BLOCKED and pab["none"] == sc.ALL_BLOCKED
    assert len(c["sns"].sent) == 0  # no TOPIC_ARN set
    sc.handler({"action": "restore"}, Ctx(), clients=c)
    assert snapshot(c) == before
    assert c["ssm"].p == {}


def test_second_freeze_keeps_original_settings():
    c = world()
    before = snapshot(c)
    sc.handler({"action": "freeze"}, Ctx(), clients=c)
    sc.handler({"action": "freeze"}, Ctx(), clients=c)
    sc.handler({"action": "restore"}, Ctx(), clients=c)
    assert snapshot(c) == before


def test_restore_tolerates_deleted_schedule():
    c = world()
    sc.handler({"action": "freeze"}, Ctx(), clients=c)
    del c["scheduler"].items["one-time"]  # one-time schedule deleted itself meanwhile
    sc.handler({"action": "restore"}, Ctx(), clients=c)
    assert c["scheduler"].items["every-minute"]["State"] == "ENABLED"
    assert c["ssm"].p == {}


def test_restore_with_nothing_frozen():
    c = world()
    r = sc.handler({"action": "restore"}, Ctx(), clients=c)
    assert r["note"] == "nothing frozen"


def test_emails_summary_when_topic_set():
    c = world()
    os.environ["TOPIC_ARN"] = "arn:topic"
    try:
        sc.handler({"action": "freeze"}, Ctx(), clients=c)
    finally:
        del os.environ["TOPIC_ARN"]
    assert len(c["sns"].sent) == 1
    assert "frozen" in c["sns"].sent[0]["Subject"]
    assert "restore" in c["sns"].sent[0]["Message"]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
