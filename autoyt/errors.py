"""Exception types that drive retry behavior."""


class ActionNeeded(Exception):
    """Needs the user (expired login, bad key). Never retried; emails once."""


class SourceRejected(Exception):
    """This video is unusable. Pick another one, no waiting."""


class UploadLimit(Exception):
    """YouTube refused more uploads today. Stop, requeue the rest."""


class StepFailed(Exception):
    """A step ran out of retries."""
