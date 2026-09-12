# Local notifications

The expanded API described here is in the development source and local preview
wheels. It has not been published to PyPI yet; `0.1.0a3` has the original interval
scheduling API. Use a development build to try the additions below.

The top-level `notification` module uses Apple's UserNotifications framework on
iOS and macOS. These are local reminders managed by the operating system; no
push server is involved. Run scripts with `python script.py` in a host that
already provides an app identity. On macOS, the optional `cocoa-py` launcher can
provide that identity when the interpreter lacks one.

Importing the module neither prompts for permission nor installs a notification
delegate. All identifiers are scoped to **the library within the host app**,
not to an individual script. Prefer a script-specific ID prefix if scripts
should manage separate schedules.

## Permission and settings

```python
import notification

if not notification.available():
    raise RuntimeError("This interpreter needs an app identity for notifications")

status = notification.permission()  # Does not prompt.
if status == "not_determined":
    status = notification.request_permission(timeout=120)
print(notification.settings())
```

`available()` checks app identity only. `permission()` and
`request_permission(*, timeout=120)` return `not_determined`, `denied`,
`authorized`, `provisional`, or iOS `ephemeral` (`unknown` for a future OS value).
The explicit request asks for alerts, sounds and badges. Previously denied
permission must be changed in system settings; repeated requests do not force
another prompt. Querying or removing notices does not request authorization.

`settings()` returns a dictionary:

| Key | Values |
| --- | --- |
| `authorization` | The status returned by `permission()`. |
| `alert`, `sound`, `badge` | `enabled`, `disabled`, `not_supported`, or `unknown`. |
| `notification_center`, `lock_screen` | The same individual setting values. |
| `scheduled_delivery`, `time_sensitive` | The same values; querying does not opt a notice into either feature. |
| `alert_style` | `none`, `banner`, `alert`, or `unknown`. |
| `previews` | `always`, `when_authenticated`, `never`, or `unknown`. |

Authorization alone does not guarantee banners or sound. Focus, scheduled
summaries, provisional permission and individual system settings still apply.

## Schedule

```python
schedule(title, body="", *, subtitle="", delay=None, repeat=False,
         at=None, calendar=None, sound=True, foreground=True, identifier=None)
```

Returns the supplied ID or a generated UUID. IDs contain 1–128 UTF-16 code units;
use the returned ID with all management APIs, without the native namespace.
Reusing an ID replaces its **pending** notice. It does not promise to clear an
already delivered copy. Titles, subtitles and bodies are strings. `sound=True`
requests the default system sound; `False` is silent.

Choose one trigger:

| Arguments | Meaning |
| --- | --- |
| None specified | Fire after 1 second, preserving the original API default. |
| `delay=0` | Request immediate delivery. |
| `delay=seconds` | Fire after a positive interval, at most 31,536,000 seconds. |
| `delay=seconds, repeat=True` | Repeat the interval; at least 60 seconds. |
| `at=aware_datetime` | Fire once at a future instant, rounded up to a whole second. |
| `calendar=CalendarTrigger(...)` | Repeat at a daily or weekly wall-clock time. |

`delay`, `at` and `calendar` are mutually exclusive. `repeat` applies only to
intervals: calendar triggers already repeat. A naive datetime is rejected;
choose a timezone explicitly rather than depending on an ambiguous local time.

```python
from datetime import datetime, timedelta, timezone
from notification import CalendarTrigger, schedule

schedule("Ready", "The export has finished", delay=0, sound=False)
schedule("Timer", "Take a break", subtitle="Desk reminder", delay=600,
         identifier="desk:break")
schedule("Check-in", at=datetime.now(timezone.utc) + timedelta(hours=2))

# Every day at 08:30 in the device's local timezone.
schedule("Morning", calendar=CalendarTrigger(hour=8, minute=30),
         identifier="desk:morning")

# Monday at 09:00 in a fixed named timezone, including its seasonal rules.
schedule("Weekly review", calendar=CalendarTrigger(
    hour=9, weekday=0, timezone="America/New_York"), identifier="desk:weekly")
```

`CalendarTrigger(*, hour, minute=0, second=0, weekday=None, timezone=None)` is
immutable. Hours use 0–23, minutes/seconds 0–59. Weekdays use Monday=0 through
Sunday=6; `None` means daily. A missing timezone follows the device's local
timezone. A named IANA timezone remains anchored to that zone when travelling.
Apple's calendar matching determines behavior at daylight-saving transitions,
including skipped or repeated wall-clock times. A calendar day is not the same
as a fixed 86,400-second interval. To express an unambiguous one-off instant at
a clock change, use an aware datetime with the intended offset/fold.

Successful scheduling means the OS accepted the request, not that a banner was
shown. Delivery can occur after the script exits. If scheduling is interrupted
or times out after submission, the OS may still accept it. Supply an ID when
you need to query, replace or cancel such a request later; an abandoned waiter
does not blindly delete a newer schedule with the same ID.

## Inspect and remove

`pending()` returns pending library requests. `delivered()` returns library
notices **currently present in Notification Center**. It is not a complete
history, proof of visual presentation, or a read-receipt API. Dismissed notices
disappear, and suppressed notices may never appear there.

Both return dictionaries with `identifier`, `title`, `subtitle`, `body`,
`sound`, `foreground`, `repeat`, and `trigger`. Trigger dictionaries contain:

| `kind` | Other fields |
| --- | --- |
| `immediate` | None. |
| `interval` | `seconds`, `repeat`. |
| `date` | `timestamp`, a Unix timestamp. |
| `calendar` | `hour`, `minute`, `second`, `weekday` and `timezone`; the last two may be `None`. |
| `unknown` | A trigger type created outside this API. |

Pending entries add `next_date` (Unix timestamp or `None`); delivered entries add
`delivered_at` (Unix timestamp). Lists are snapshots with no ordering guarantee.

| Operation | Scope |
| --- | --- |
| `cancel_pending(id)` | Remove that pending library notice. |
| `cancel_pending()` | Remove all currently pending library notices. |
| `remove_delivered(id)` | Remove that delivered library notice. |
| `remove_delivered()` | Remove all currently delivered library notices. |
| `cancel(id)` | Remove that ID from both sets; compatible with the original API. |
| `cancel_all()` | Cancel currently pending library IDs, also removing their delivered copies; keeps its original behavior. |

To clear both sets completely, call `cancel_pending()` and `remove_delivered()`.
Other host notices are preserved. Removal is asynchronous at the OS boundary;
the next query can briefly see an old snapshot. Bulk removal covers IDs observed
by that operation, not schedules created concurrently afterwards.

## Host integration

`foreground=True` requests foreground banners/list entries, with sound only if
the notice has a sound. `foreground=False` requests no foreground presentation;
it does not suppress background delivery. This preference requires a cooperating
host delegate. Pythona's integration and cocoa-py's optional macOS launcher
implement it. The same Python calls work on both platforms; the host code uses
its own application lifecycle.

The host must assign and retain its `UNUserNotificationCenterDelegate` before
launch completes. **Do not replace an existing delegate from a Python import.**
Merge the following policy into the host's `willPresent` method, preserving its
existing behavior for other identifiers:

```swift
// Within the host's UNUserNotificationCenterDelegate implementation:
nonisolated func userNotificationCenter(
    _ center: UNUserNotificationCenter,
    willPresent notification: UNNotification,
    withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
) {
    let request = notification.request
    guard request.identifier.hasPrefix("cocoa-py:") else {
        completionHandler([])  // Use the host's policy for its other notices.
        return
    }
    if let foreground = request.content.userInfo["cocoa-py.foreground"] as? NSNumber,
       !foreground.boolValue {
        completionHandler([])
        return
    }
    var options: UNNotificationPresentationOptions = [.banner, .list]
    if request.content.sound != nil { options.insert(.sound) }
    completionHandler(options)
}
```

Objective-C hosts can use `CocoaPyNotificationPresentation(request)` from
`native/common/CocoaPyNotifications.h` for the same policy. The header does not
register a delegate. The native request namespace is `cocoa-py:`; the optional
boolean `content.userInfo["cocoa-py.foreground"]` defaults to true for older
notices. The callback does not execute Python or require the GIL.

Tap handling remains with the host; the library does not automatically start a
script. Attachments, actions, custom sounds, location triggers, badges, remote
push and native payload extensions are outside this convenience API. Advanced
integrations can use the platform SDK through Rubicon without creating a second
delegate that competes with the host.

## Validation boundaries

The automated native contracts exercise Apple's trigger/content objects with a
fake notification service: they cover timezone and weekday conversion, argument
validation, permission handling, namespaces, replacements, waiter lifetimes and
foreground options. They do not exercise OS delivery or claim that a banner was
visible. Device validation must cover the host's actual authorization and
foreground lifecycle. Relevant Apple references:

- [Calendar notification triggers](https://developer.apple.com/documentation/usernotifications/uncalendarnotificationtrigger)
- [Notification settings](https://developer.apple.com/documentation/usernotifications/unnotificationsettings)
- [Currently delivered notifications](https://developer.apple.com/documentation/usernotifications/unusernotificationcenter/getdeliverednotifications(completionhandler:))
- [Notification center delegate](https://developer.apple.com/documentation/usernotifications/unusernotificationcenterdelegate)
