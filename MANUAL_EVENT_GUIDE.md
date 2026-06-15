**Adding Local Events to the PTCG Calendar**

Use this guide when adding store locals, one-off tournaments, or any event not automatically synced.

**Title Format**
```
<Shop Name> - <Event Type>
<Shop Name> - <Tag> - <Event Type>
```
Each ` - ` segment is checked against the Discord mentions file. Any segment that matches fires a ping; non-matching segments appear as plain text. Use extra segments to add community pings (e.g. a format or league tag).

Segment spelling must match exactly what's on file — capitalization and spacing included — or the ping won't fire.

The last segment is the event description and is freeform.
Examples: `Game Haven - 1K Open` | `Game Haven - Locals` | `Atomic Empire - GLC - Tournament`

**Duration**
Set start and end time based on the expected run time. If unsure, 3 hours is a safe default.

**Location**
Put the street address in the Location field.

**Colors**
Tomato - Bigger standard event
Flamingo - Weekly standard locals
Sage - Alternative format free play
Basil - Alternative format tournament

**Before saving, check:**
- [ ] Title follows `<Shop> - <Event Type>` (or `<Shop> - <Tag> - <Event Type>` for multi-ping)
- [ ] Each ping segment spelling matches exactly what's in the mentions file
- [ ] Correct color set
- [ ] Start/end time correct (check AM/PM)
- [ ] Street address in Location field