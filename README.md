# iCloud Calendar (CalDAV) for Home Assistant

Edit your **iCloud calendars** in Home Assistant's Calendar panel, including **single occurrences of repeating events**.

Home Assistant's built-in CalDAV integration can show events and add new ones, but it can't change or delete them. This integration is a drop-in replacement for it: same calendars, same entities, plus editing. It was built and tested against iCloud and works with any CalDAV server that names objects by UID or allows a UID search.

## Features

- **Change and delete events** from the Calendar panel and with the `calendar.update_event` / `calendar.delete_event` actions.
- **Repeating events done properly.** When you edit or delete an occurrence, Home Assistant asks *Only this event* or *This and all future events*:
  - *Only this event* changes just that day (an override with `RECURRENCE-ID`) or skips it (`EXDATE`). The rest of the series stays as it was.
  - *This and all future events* ends the series the day before and starts a new one with your changes. On the first occurrence it changes or deletes the whole series.
  - All-day, time-zone and floating events are supported, including series that cross a daylight saving change.
- **Nothing else gets lost.** Alarms, attendees, URLs, Apple's own properties and changes to other occurrences are kept. Only the fields the editor shows are changed (title, start, end, description, location, repeat rule).
- **Safe against parallel changes.** Every write sends the object's ETag. If the event was changed on your iPhone or Mac in the meantime, the server refuses the write and Home Assistant tells you to reload, so nothing is overwritten.
- **Repeat rule shown in the editor.** Occurrences carry their series' rule, so the panel shows "Every week on Tuesday" and offers the scope choice.
- **Read-only calendars stay read-only.** Shared calendars you can't write to don't get edit or delete buttons.
- **Reminders** (to-do lists) as in the built-in integration: add, change, complete and delete to-dos. On iCloud, changing a to-do works here too; with Home Assistant 2026.11 the built-in integration fails there with `412 Precondition Failed`.
- **Easy switch.** If the built-in CalDAV integration is set up, the account can be copied from it, so you don't need to type the password again.
- **Translations:** English and Polish.

## Installation

### Marketplace (HACS)

1. Settings → Marketplace → ⋮ → Custom repositories → add `https://github.com/tomek7667/home-assistant-icloud-calendar` as an *Integration*.
2. Install **iCloud Calendar (CalDAV)** and restart Home Assistant.

### Manual

Copy `custom_components/icloud_calendar` into your `config/custom_components/` folder and restart Home Assistant.

## Setup

**Settings → Devices & services → Add integration → iCloud Calendar (CalDAV)**

- **Copy the settings of the built-in CalDAV integration**: pick the CalDAV entry. Server, account and password are copied.
- **Enter the server and account**: for iCloud the server is `https://caldav.icloud.com/`, the username is your Apple Account email, and the password is an **app-specific password** (account.apple.com → Sign-In and Security → App-Specific Passwords). Your normal Apple password won't work.

Every calendar becomes a `calendar.*` entity and every reminders list a `todo.*` entity.

## Switching from the built-in CalDAV integration

Dashboards and automations keep working if the new entities get the old entity IDs:

1. Add this integration with *Copy the settings of the built-in CalDAV integration*. The new entities get a `_2` suffix (`calendar.home_2`) because the old ones still exist.
2. Check that the new calendars work, then delete the built-in **CalDAV** entry (Settings → Devices & services → CalDAV → ⋮ → Delete).
3. Rename each new entity back to its old ID (entity settings → Entity ID: `calendar.home_2` → `calendar.home`). Names and colors come from iCloud, so they stay the same.

## How edits are written

A repeating event is one calendar object on the server: the series (with its `RRULE`) and any changed occurrences. The integration loads that object, changes it and saves it back:

| You do | Written to the server |
|---|---|
| Change a single event | The event, `SEQUENCE` increased |
| Delete a single event | The object is deleted |
| Change *only this* occurrence | An override `VEVENT` with `RECURRENCE-ID` (alarms copied from the series) |
| Delete *only this* occurrence | `EXDATE` on the series; an override for that day is removed |
| Change *this and all future* | Series gets `UNTIL` the day before; a new series (new UID) starts with the changes; later overrides move to it |
| Delete *this and all future* | Series gets `UNTIL` the day before (or the object is deleted if it's the first occurrence) |
| Change the time of a whole series | Overrides that no longer match an occurrence are dropped |

`COUNT` series are converted to `UNTIL` when split, so the two parts together keep the original number of occurrences.

## Limitations

- **Only the fields of Home Assistant's editor can be changed.** Alarms, attendees, travel time, attachments and URLs are kept but can't be edited here.
- **Invitations haven't been tested.** Editing an event someone invited you to changes your copy; don't expect the organizer or other attendees to be notified.
- **Changes from other devices show up on the next fetch.** The Calendar panel reads iCloud each time it's opened or moved to another week. The entity state (next event) is refreshed every 15 minutes, as in the built-in integration.
- **"All events" of a series** is reached with *This and all future events* on the first occurrence. Home Assistant's dialog has no separate option for it.
- **iCloud reminders:** iCloud only offers its old reminders list over CalDAV (it shows up as "Reminders ⚠️"). Lists in the current Reminders app can't be reached this way.
- **No YAML.** The `calendar: - platform: caldav` YAML setup isn't supported. Use the config flow.
- iCloud refuses CalDAV's UID search (`412 Precondition Failed`), so events are found by the URL seen when listing them or by `<UID>.ics`, which is how iCloud and Apple clients name them. On other servers a UID search is used, then a full scan as the last resort.

## Development

The iCalendar editing logic lives in `custom_components/icloud_calendar/editing.py` and doesn't depend on Home Assistant. It has its own tests:

```sh
pip install -r requirements-test.txt
pytest tests
```

## License

Apache License 2.0. This integration is derived from Home Assistant's [CalDAV integration](https://github.com/home-assistant/core/tree/dev/homeassistant/components/caldav). See [NOTICE](NOTICE).
