# Location and compass headings

`location` provides foreground position fixes, compass readings, bounded update
streams, and Apple's network geocoder. Importing the module does not start
sensors or request permission. `status()` and `heading_available()` are queries.

## Position and freshness

```python
import location

position = location.current(accuracy=10, max_age=0, timeout=30)
print(position.latitude, position.longitude, position.horizontal_accuracy)

with location.watch(distance_filter=5, capacity=16) as updates:
    sample = updates.read(timeout=10)
    if sample is not None:
        print(sample.latitude, sample.longitude)
```

Coordinates use degrees; altitude, distance filters and accuracy use meters.
Speed uses meters per second. `course` is the direction of travel, not the edge
of the device that points north. Unavailable altitude, speed and course values
are `None`. `accuracy` requests a level of precision; inspect the returned
`horizontal_accuracy` rather than assuming the request guarantees it.

`current()` selects the newest valid fix in a delivered batch. A watch preserves
the chronological order within the batch. Invalid fixes are discarded.

For both position and heading requests, `max_age` is in seconds:

- A positive value accepts samples that are no older than that limit when
  delivered. The supported range ends at 86400 seconds.
- Zero accepts only samples measured after this request starts its updates.
  The start time is recorded after any permission prompt. Normal delivery
  latency does not invalidate those new samples.

The default is 15 seconds for positions and 5 seconds for headings. `max_age`
does not control the frequency of updates. `timeout` limits waiting, including
time spent awaiting permission. Failure to obtain a valid reading by that
deadline raises `TimeoutError` and the single-sample call stops its sensors.

## Compass

```python
import location

if location.heading_available():
    direction = location.heading(max_age=0)
    print(direction.magnetic_heading, direction.accuracy)
```

`Heading` is an immutable record with four fields:

| Field | Meaning |
| --- | --- |
| `magnetic_heading` | Degrees clockwise from magnetic north, from 0 up to but excluding 360. |
| `true_heading` | Degrees clockwise from geographic north, or `None` when unavailable. |
| `accuracy` | Estimated angular error in degrees; smaller is better. |
| `timestamp` | Measurement time as Unix seconds. |

The default magnetic mode does not request location permission or start
position updates. To require geographic north, use `true_north=True`:

```python
direction = location.heading(true_north=True, timeout=30)
print(direction.true_heading)
```

True-north mode requests foreground location access if necessary and runs
position updates on the same native manager. It withholds readings until their
true heading is valid. Permission refusal raises `PermissionError`. Poor
reception or unreliable magnetic data may still lead to a timeout.

Compass hardware is available on supported iOS devices. Native macOS reports
no compass support. Check `heading_available()` first; starting an unsupported
heading request raises `NotImplementedError`. Magnetic interference and
calibration affect accuracy. The library drops readings marked invalid by the
system and does not open calibration UI itself.

## Heading streams and orientation

```python
with location.watch_heading(angle_filter=1, capacity=16) as updates:
    for _ in range(10):
        sample = updates.read(timeout=2)
        if sample is not None:
            print(sample.magnetic_heading)
```

`angle_filter` specifies the minimum angular change required for another event,
from 0 through 180 degrees. Zero requests every available update; it does not
guarantee an update rate. `HeadingWatch` supports the same `true_north`,
`max_age`, and `orientation` options as `heading()`.

The reference orientation defaults to `"portrait"`, where headings refer to
the device's top edge. Other values are `"portrait_upside_down"`,
`"landscape_left"`, and `"landscape_right"`, following Core Location's physical
device orientations. They are fixed for the request, not automatically changed
when a host window rotates. Landscape left places the device's top edge on the
left; landscape right places it on the right.

Both watch classes use the common `read(timeout)`, `stats`, and `close()`
contract. A read timeout returns `None` and leaves the watch active. Capacity
is an integer from 1 through 4096; a full queue drops its oldest sample and
increments `stats["dropped"]`. Use a context manager to stop all sensors when
leaving a loop. Closing also clears queued samples and detaches the native
manager so late callbacks cannot enqueue more data.

## Addresses and host integration

`geocode(address)` and `reverse_geocode(latitude, longitude)` return `Place`
records. They contact Apple's regional geocoder and do not request the device's
location. Avoid geocoding on every sensor event; reuse results where appropriate.

Addresses must contain non-whitespace text and no NUL characters. Reverse
geocoding accepts finite numeric coordinates in the latitude/longitude ranges;
booleans are rejected. Optional address fields can be `None`. Service errors
raise `OSError`, and a deadline raises `TimeoutError`. Both cancel and release
the underlying request. Results depend on the regional service, language,
network availability, and rate limits; an address need not resolve uniquely.

The current backend remains `CLGeocoder` for the supported iOS 17 and macOS 14
deployment targets. Apple deprecated it in version 26, but has not removed it.
The newer MapKit request APIs require version 26 and do not directly provide
all of the structured fields exposed by `Place`. Keeping the current backend
avoids adding two implementations without eliminating that compatibility
dependency. Revisit this choice when the deployment targets or Apple API
availability change; deprecation alone does not require a public API change.

## Permission and execution boundaries

`status()` reports the current system state without requesting access. Denied
or restricted position access raises `PermissionError`; granting permission
later requires a new request after the failed one has been closed. When a
running request loses authorization, it stops updates and reports the error.
Queries do not bypass an app's usage-description or authorization requirements.

Reduced Accuracy is supported: `status()["precise"]` is `False`, and fixes carry
the actual `horizontal_accuracy`. Requesting a small `accuracy` does not upgrade
authorization. Coarse fixes can be less frequent, so a strict `max_age` may
cause a timeout. The library does not request temporary Full Accuracy access.

Single-sample calls release their sensors on success, timeout, or an exception.
A stream read timeout returns `None` and keeps the stream open; use `with` or
`close()` to end it. Closing cancels pending permission and geocoder work at
the request level and ignores late callbacks; it cannot withdraw a system
permission dialog that is already on screen.

Position and true-north requests require the host's location usage description
and system authorization. See [iOS embedding](embedding.md) and
[macOS execution](macos.md). These APIs do not enable background location or
request Always authorization. A host's permission to run code in the background
does not itself establish a background location session.

Start permission requests in the foreground. With the current foreground API,
updates may stop when the app leaves the foreground, and starting a new request
there is not guaranteed to produce a fix. The system can reject that request
with `PermissionError` even when `permission()` reports `"authorized"`: When
In Use authorization is not permission to start location services at any time.
A host that needs location for background work should obtain it in the
foreground and explicitly pass the position and its timestamp to that work.
A single-sample deadline still limits
the wait while Python is executing, and returning to the foreground permits a
new request. If iOS suspends the entire process, Python cannot run timeout or
cleanup code until execution resumes. Hosts should close streams when their
own foreground work ends instead of using location to keep arbitrary scripts
alive. Guaranteed background tracking, geofencing, and Always authorization are
outside this API's contract.

Apple's [background location configuration](https://developer.apple.com/documentation/corelocation/cllocationmanager/allowsbackgroundlocationupdates)
and [When In Use authorization](https://developer.apple.com/documentation/corelocation/cllocationmanager/requestwheninuseauthorization%28%29)
describe these lifecycle restrictions. A
[continued processing task](https://developer.apple.com/documentation/backgroundtasks/bgcontinuedprocessingtask)
provides execution time, not additional location authorization.
