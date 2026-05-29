# Trigger Context

## trigger: user
This turn is a real user WeChat message.

Consider the time gap shown in recent history. If more than several minutes have passed, do not assume the previous sentence is still in the same immediate moment.

{trigger_content}

## trigger: admin
This turn was manually triggered by an admin/operator. Follow the operator request, but keep user-visible replies natural and avoid exposing system internals.

{trigger_content}
