"""Spend-cap kill switch for a small AWS account.

AWS has no hard spending limit. This Lambda is the "stop" half of one: an AWS
Budget publishes to an SNS topic when actual spend passes the limit, SNS invokes
this function, and it freezes everything in its region that can run up a bill:

* every ENABLED EventBridge Scheduler schedule is set to DISABLED;
* every other Lambda function gets reserved concurrency 0 (nothing can invoke
  it, including public function URLs);
* every S3 bucket gets Block Public Access turned fully on (stops anonymous
  downloads of public objects).

Nothing is deleted. What was changed, and the prior settings, are saved in SSM
(`STATE_PARAM`, default /spend-cap/frozen) so `{"action": "restore"}` undoes
exactly that. `{"action": "freeze", "dry_run": true}` reports without changing.

Env: STATE_PARAM (optional), TOPIC_ARN (optional, email summary).
"""
import json
import os

STATE_PARAM = os.environ.get("STATE_PARAM", "/spend-cap/frozen")
ALL_BLOCKED = {"BlockPublicAcls": True, "IgnorePublicAcls": True,
               "BlockPublicPolicy": True, "RestrictPublicBuckets": True}
SCHEDULE_FIELDS = ("Name", "GroupName", "ScheduleExpression", "ScheduleExpressionTimezone",
                   "StartDate", "EndDate", "Description", "FlexibleTimeWindow", "Target",
                   "KmsKeyArn", "ActionAfterCompletion")


def _clients():
    import boto3
    return {n: boto3.client(n) for n in ("scheduler", "lambda", "s3", "ssm", "sns")}


def _not_found(e, *codes):
    code = getattr(e, "response", {}).get("Error", {}).get("Code", "")
    return code in codes


def _set_schedule_state(sch, group, name, state):
    cur = sch.get_schedule(GroupName=group, Name=name)
    if cur.get("State") == state:
        return False
    args = {k: cur[k] for k in SCHEDULE_FIELDS if cur.get(k) is not None}
    args["State"] = state
    sch.update_schedule(**args)
    return True


def _all_schedules(sch):
    out, token = [], None
    while True:
        r = sch.list_schedules(**({"NextToken": token} if token else {}))
        out += r.get("Schedules", [])
        token = r.get("NextToken")
        if not token:
            return out


def _all_functions(lam):
    out, marker = [], None
    while True:
        r = lam.list_functions(**({"Marker": marker} if marker else {}))
        out += r.get("Functions", [])
        marker = r.get("NextMarker")
        if not marker:
            return out


def _load_state(ssm):
    try:
        return json.loads(ssm.get_parameter(Name=STATE_PARAM)["Parameter"]["Value"])
    except Exception as e:
        if _not_found(e, "ParameterNotFound"):
            return None
        raise


def _save_state(ssm, state):
    ssm.put_parameter(Name=STATE_PARAM, Value=json.dumps(state, separators=(",", ":")),
                      Type="String", Overwrite=True)


def freeze(c, self_name, dry_run=False):
    # Merge with an earlier freeze so a second freeze never overwrites the
    # original settings with already-frozen ones.
    state = _load_state(c["ssm"]) or {"schedules": [], "lambdas": {}, "buckets": {}}
    done = {"schedules": [], "lambdas": [], "buckets": []}

    for s in _all_schedules(c["scheduler"]):
        if s.get("State") != "ENABLED":
            continue
        key = [s.get("GroupName", "default"), s["Name"]]
        done["schedules"].append("/".join(key))
        if not dry_run and _set_schedule_state(c["scheduler"], key[0], key[1], "DISABLED"):
            if key not in state["schedules"]:
                state["schedules"].append(key)
            _save_state(c["ssm"], state)  # save as we go, so a crash can still be undone

    for f in _all_functions(c["lambda"]):
        name = f["FunctionName"]
        if name == self_name:
            continue
        prior = c["lambda"].get_function_concurrency(FunctionName=name).get("ReservedConcurrentExecutions")
        if prior == 0:
            continue
        done["lambdas"].append(name)
        if not dry_run:
            state["lambdas"].setdefault(name, prior)  # None = had no reservation
            _save_state(c["ssm"], state)
            c["lambda"].put_function_concurrency(FunctionName=name, ReservedConcurrentExecutions=0)

    for b in c["s3"].list_buckets().get("Buckets", []):
        name = b["Name"]
        try:
            prior = c["s3"].get_public_access_block(Bucket=name)["PublicAccessBlockConfiguration"]
        except Exception as e:
            if not _not_found(e, "NoSuchPublicAccessBlockConfiguration"):
                raise
            prior = None
        if prior == ALL_BLOCKED:
            continue
        done["buckets"].append(name)
        if not dry_run:
            state["buckets"].setdefault(name, prior)
            _save_state(c["ssm"], state)
            c["s3"].put_public_access_block(Bucket=name, PublicAccessBlockConfiguration=ALL_BLOCKED)

    return done


def restore(c, dry_run=False):
    state = _load_state(c["ssm"])
    if not state:
        return {"note": "nothing frozen"}
    done = {"schedules": [], "lambdas": [], "buckets": []}
    for group, name in list(state["schedules"]):
        done["schedules"].append(f"{group}/{name}")
        if dry_run:
            continue
        try:
            _set_schedule_state(c["scheduler"], group, name, "ENABLED")
        except Exception as e:
            if not _not_found(e, "ResourceNotFoundException"):
                raise
        state["schedules"].remove([group, name])
        _save_state(c["ssm"], state)
    for name, prior in list(state["lambdas"].items()):
        done["lambdas"].append(name)
        if dry_run:
            continue
        try:
            if prior is None:
                c["lambda"].delete_function_concurrency(FunctionName=name)
            else:
                c["lambda"].put_function_concurrency(FunctionName=name, ReservedConcurrentExecutions=prior)
        except Exception as e:
            if not _not_found(e, "ResourceNotFoundException"):
                raise
        del state["lambdas"][name]
        _save_state(c["ssm"], state)
    for name, prior in list(state["buckets"].items()):
        done["buckets"].append(name)
        if dry_run:
            continue
        try:
            if prior is None:
                c["s3"].delete_public_access_block(Bucket=name)
            else:
                c["s3"].put_public_access_block(Bucket=name, PublicAccessBlockConfiguration=prior)
        except Exception as e:
            if not _not_found(e, "NoSuchBucket"):
                raise
        del state["buckets"][name]
        _save_state(c["ssm"], state)
    if not dry_run:
        c["ssm"].delete_parameter(Name=STATE_PARAM)
    return done


def _summary(action, done, dry_run):
    verb = {"freeze": "Froze", "restore": "Restored"}[action]
    if dry_run:
        verb = f"Dry run, would {action}"
    if "note" in done:
        return done["note"]
    lines = [f"{verb}:"]
    for k, label in (("schedules", "Schedules"), ("lambdas", "Lambda functions"), ("buckets", "S3 buckets")):
        lines.append(f"- {label}: {', '.join(done.get(k, [])) or 'none'}")
    if action == "freeze" and not dry_run:
        lines += ["", "Nothing was deleted. To undo, invoke the spend-cap-killswitch Lambda with",
                  '{"action": "restore"}. Raise the budget first if spend is still over it.']
    return "\n".join(lines)


def handler(event, context, clients=None):
    event = event or {}
    if "Records" in event:  # from SNS (the budget alert)
        action, dry_run = "freeze", False
    else:
        action = event.get("action", "freeze")
        dry_run = bool(event.get("dry_run"))
    if action not in ("freeze", "restore"):
        raise ValueError(f"unknown action {action!r}")
    c = clients or _clients()
    self_name = getattr(context, "function_name", None) or os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    done = freeze(c, self_name, dry_run) if action == "freeze" else restore(c, dry_run)
    text = _summary(action, done, dry_run)
    print(text)
    topic = os.environ.get("TOPIC_ARN")
    if topic and not dry_run and "note" not in done:
        subject = "AWS spend cap hit: account frozen" if action == "freeze" else "AWS spend cap: account restored"
        c["sns"].publish(TopicArn=topic, Subject=subject, Message=text)
    return {"action": action, "dry_run": dry_run, **done}
