"""This module contains classes relating to Sonos Alarms."""
from __future__ import annotations

import logging
import re
from datetime import datetime

from . import discovery
from .core import _SocoSingletonBase, PLAY_MODES, SoCo
from .exceptions import SoCoException
from .xml import XML

log = logging.getLogger(__name__)  # pylint: disable=C0103
TIME_FORMAT = "%H:%M:%S"


def is_valid_recurrence(text):
    """Check that ``text`` is a valid recurrence string.

    A valid recurrence string is  ``DAILY``, ``ONCE``, ``WEEKDAYS``,
    ``WEEKENDS`` or of the form ``ON_DDDDDD`` where ``D`` is a number from 0-6
    representing a day of the week (Sunday is 0), e.g. ``ON_034`` meaning
    Sunday, Wednesday and Thursday

    Args:
        text (str): the recurrence string to check.

    Returns:
        bool: `True` if the recurrence string is valid, else `False`.

    Examples:

        >>> from soco.alarms import is_valid_recurrence
        >>> is_valid_recurrence('WEEKENDS')
        True
        >>> is_valid_recurrence('')
        False
        >>> is_valid_recurrence('ON_132')  # Mon, Tue, Wed
        True
        >>> is_valid_recurrence('ON_666')  # Sat
        True
        >>> is_valid_recurrence('ON_3421') # Mon, Tue, Wed, Thur
        True
        >>> is_valid_recurrence('ON_123456789') # Too many digits
        False
    """
    if text in ("DAILY", "ONCE", "WEEKDAYS", "WEEKENDS"):
        return True
    return re.search(r"^ON_[0-6]{1,7}$", text) is not None


class Alarms(_SocoSingletonBase):
    """A class representing all known Sonos Alarms.

    Is a singleton and every `Alarms()` object will return the same instance.

    Example use:

        >>> alarms = Alarms()
        >>> alarms.update()
        >>> alarms.alarms
        {469: <Alarm id:469@22:07:41 at 0x7f5198797dc0>,
         470: <Alarm id:470@22:07:46 at 0x7f5198797d60>}
        >>> for alarm in alarms:
        ...     alarm
        ...
        <Alarm id:469@22:07:41 at 0x7f5198797dc0>
        <Alarm id:470@22:07:46 at 0x7f5198797d60>
        >>> alarms[470]
        <Alarm id:470@22:07:46 at 0x7f5198797d60>
        >>> new_alarm = Alarm(zone)
        >>> new_alarm.save()
        471
        >>> new_alarm.recurrence = "ONCE"
        >>> new_alarm.save()
        471
        >>> alarms.alarms
        {469: <Alarm id:469@22:07:41 at 0x7f5198797dc0>,
         470: <Alarm id:470@22:07:46 at 0x7f5198797d60>,
         471: <Alarm id:471@22:08:40 at 0x7f51987f1b50>}
        >>> alarms[470].remove()
        >>> alarms.alarms
        {469: <Alarm id:469@22:07:41 at 0x7f5198797dc0>,
         471: <Alarm id:471@22:08:40 at 0x7f51987f1b50>}
        >>> for alarm in alarms:
        ...     alarm.remove()
        ...
        >>> a.alarms
        {}
    """

    def __init__(self):
        """Initialize the instance."""
        self.alarms: dict[int, Alarm] = {}
        self._last_zone_used: SoCo | None = None
        self._last_alarm_list_version: str | None = None
        self.last_uid: str | None = None
        self.last_id: int = 0

    @property
    def last_alarm_list_version(self):
        """Return last seen alarm list version."""
        return self._last_alarm_list_version

    @last_alarm_list_version.setter
    def last_alarm_list_version(self, alarm_list_version: str):
        """Store alarm list version and store UID/ID values."""
        self.last_uid, last_id = alarm_list_version.split(":")
        self.last_id = int(last_id)
        self._last_alarm_list_version = alarm_list_version

    def __getitem__(self, alarm_id: int):
        """Return the alarm by ID."""
        return self.alarms.get(alarm_id)

    def __iter__(self):
        """Return an interator for all alarms."""
        for alarm in list(self.alarms.values()):
            yield alarm

    def remove_by_id(self, alarm_id: int):
        """Remove an alarm using its identifier.

        Returns:
            bool: True if alarm is found and removed, False otherwise.
        """
        alarm = self.alarms.get(alarm_id)
        if not alarm:
            return False

        alarm.remove()
        return True

    def update(self, zone: SoCo | None = None):
        """Update all alarms and current alarm list version.

        Raises:
            SoCoException: If the `CurrentAlarmListVersion` value is unexpected.
                May occur if the provided zone is from a different household.
        """
        if zone is None:
            zone = self._last_zone_used or discovery.any_soco()

        self._last_zone_used = zone

        response = zone.alarmClock.ListAlarms()
        current_alarm_list_version = response["CurrentAlarmListVersion"]

        if self.last_alarm_list_version:
            alarm_list_uid, alarm_list_id = current_alarm_list_version.split(":")
            if self.last_uid != alarm_list_uid:
                matching_zone = next(
                    (z for z in zone.all_zones if z.uid == alarm_list_uid), None
                )
                if not matching_zone:
                    raise SoCoException(
                        "Alarm list UID {} does not match {}".format(
                            current_alarm_list_version, self.last_alarm_list_version
                        )
                    )

            if int(alarm_list_id) <= self.last_id:
                return

        self.last_alarm_list_version = current_alarm_list_version

        alarms = parse_alarm_payload(response, zone)

        # Replace Alarm objects if updated
        for alarm in alarms:
            if alarm != self.alarms.get(alarm.alarm_id):
                self.alarms[alarm.alarm_id] = alarm

        # Prune alarms removed externally
        for alarm_id in list(self.alarms):
            match = next((a for a in alarms if a.alarm_id == alarm_id), None)
            if not match:
                self.alarms.pop(alarm_id)

class Alarm:

    """A class representing a Sonos Alarm.

    Alarms may be created or updated and saved to, or removed from the Sonos
    system. An alarm is not automatically saved. Call `save()` to do that.
    """

    # pylint: disable=too-many-instance-attributes
    # pylint: disable=too-many-arguments
    def __init__(
        self,
        zone: SoCo,
        start_time: datetime.time | None = None,
        duration: datetime.time | None = None,
        recurrence: str = "DAILY",
        enabled: bool = True,
        program_uri: str = None,
        program_metadata: str = "",
        play_mode: str = "NORMAL",
        volume: int = 20,
        include_linked_zones: bool = False,
    ):
        """
        Args:
            zone (`SoCo`): The soco instance which will play the alarm.
            start_time (datetime.time, optional): The alarm's start time.
                Specify hours, minutes and seconds only. Defaults to the
                current time.
            duration (datetime.time, optional): The alarm's duration. Specify
                hours, minutes and seconds only. May be `None` for unlimited
                duration. Defaults to `None`.
            recurrence (str, optional): A string representing how
                often the alarm should be triggered. Can be ``DAILY``,
                ``ONCE``, ``WEEKDAYS``, ``WEEKENDS`` or of the form
                ``ON_DDDDDD`` where ``D`` is a number from 0-6 representing a
                day of the week (Sunday is 0), e.g. ``ON_034`` meaning Sunday,
                Wednesday and Thursday. Defaults to ``DAILY``.
            enabled (bool, optional): `True` if alarm is enabled, `False`
                otherwise. Defaults to `True`.
            program_uri(str, optional): The uri to play. If `None`, the
                built-in Sonos chime sound will be used. Defaults to `None`.
            program_metadata (str, optional): The metadata associated with
                `program_uri`. Defaults to ''.
            play_mode(str, optional): The play mode for the alarm. Can be one
                of ``NORMAL``, ``SHUFFLE_NOREPEAT``, ``SHUFFLE``,
                ``REPEAT_ALL``, ``REPEAT_ONE``, ``SHUFFLE_REPEAT_ONE``.
                Defaults to ``NORMAL``.
            volume (int, optional): The alarm's volume (0-100). Defaults to 20.
            include_linked_zones (bool, optional): `True` if the alarm should
                be played on the other speakers in the same group, `False`
                otherwise. Defaults to `False`.
        """

        self.zone = zone
        if start_time is None:
            start_time = datetime.now().time().replace(microsecond=0)
        self.start_time = start_time
        self.duration = duration
        self._recurrence = recurrence
        self.enabled = enabled
        self.program_uri = program_uri
        self.program_metadata = program_metadata
        self._play_mode = play_mode
        self._volume = volume
        self.include_linked_zones = include_linked_zones
        self.alarm_id = None

    def __repr__(self):
        middle = str(self.start_time.strftime(TIME_FORMAT))
        return "<{} id:{}@{} at {}>".format(
            self.__class__.__name__, self.alarm_id, middle, hex(id(self))
        )

    def __eq__(self, other: object):
        if not isinstance(other, Alarm):
            return NotImplemented

        for attr in [
            "alarm_id",
            "enabled",
            "duration",
            "include_linked_zones",
            "play_mode",
            "program_uri",
            "program_metadata",
            "recurrence",
            "start_time",
            "volume",
            "zone",
        ]:
            if getattr(self, attr) != getattr(other, attr):
                return False
        return True

    @property
    def play_mode(self):
        """
        `str`: The play mode for the alarm.

            Can be one of ``NORMAL``, ``SHUFFLE_NOREPEAT``, ``SHUFFLE``,
            ``REPEAT_ALL``, ``REPEAT_ONE``, ``SHUFFLE_REPEAT_ONE``.
        """
        return self._play_mode

    @play_mode.setter
    def play_mode(self, play_mode: str):
        """See `playmode`."""
        play_mode = play_mode.upper()
        if play_mode not in PLAY_MODES:
            raise KeyError("'%s' is not a valid play mode" % play_mode)
        self._play_mode = play_mode

    @property
    def volume(self):
        """`int`: The alarm's volume (0-100)."""
        return self._volume

    @volume.setter
    def volume(self, volume: int | str):
        """See `volume`."""
        # max 100
        volume = int(volume)
        self._volume = max(0, min(volume, 100))  # Coerce in range

    @property
    def recurrence(self):
        """`str`: How often the alarm should be triggered.

        Can be ``DAILY``, ``ONCE``, ``WEEKDAYS``, ``WEEKENDS`` or of the form
        ``ON_DDDDDDD`` where ``D`` is a number from 0-7 representing a day of
        the week (Sunday is 0), e.g. ``ON_034`` meaning Sunday, Wednesday and
        Thursday.
        """
        return self._recurrence

    @recurrence.setter
    def recurrence(self, recurrence: str):
        """See `recurrence`."""
        if not is_valid_recurrence(recurrence):
            raise KeyError("'%s' is not a valid recurrence value" % recurrence)

        self._recurrence = recurrence

    def save(self):
        """Save the alarm to the Sonos system.

        Returns:
            str: The alarm ID, or `None` if no alarm was saved.

        Raises:
            ~soco.exceptions.SoCoUPnPException: if the alarm cannot be created
                because there
                is already an alarm for this room at the specified time.
        """
        # pylint: disable=bad-continuation
        args = [
            ("StartLocalTime", self.start_time.strftime(TIME_FORMAT)),
            (
                "Duration",
                "" if self.duration is None else self.duration.strftime(TIME_FORMAT),
            ),
            ("Recurrence", self.recurrence),
            ("Enabled", "1" if self.enabled else "0"),
            ("RoomUUID", self.zone.uid),
            (
                "ProgramURI",
                "x-rincon-buzzer:0" if self.program_uri is None else self.program_uri,
            ),
            ("ProgramMetaData", self.program_metadata),
            ("PlayMode", self.play_mode),
            ("Volume", self.volume),
            ("IncludeLinkedZones", "1" if self.include_linked_zones else "0"),
        ]
        if self.alarm_id is None:
            response = self.zone.alarmClock.CreateAlarm(args)
            self.alarm_id = int(response["AssignedID"])
            alarms = Alarms()
            alarms.alarms[self.alarm_id] = self
        else:
            # The alarm has been saved before. Update it instead.
            args.insert(0, ("ID", self.alarm_id))
            self.zone.alarmClock.UpdateAlarm(args)
        return self.alarm_id

    def remove(self):
        """Remove the alarm from the Sonos system.

        There is no need to call `save`. The Python instance is not deleted,
        and can be saved back to Sonos again if desired.
        """
        self.zone.alarmClock.DestroyAlarm([("ID", self.alarm_id)])
        alarms = Alarms()
        alarms.alarms.pop(self.alarm_id, None)
        self.alarm_id = None


def get_alarms(zone: SoCo | None = None):
    """Get a list of all alarms known to the Sonos system.

    Args:
        zone (soco.SoCo, optional): a SoCo instance to query. If None, a random
            instance is used. Defaults to `None`.

    Returns:
        list: A list of `Alarm` instances
    """
    alarms = Alarms()
    alarms.update(zone)
    return list(alarms.alarms.values())


def remove_alarm_by_id(zone, alarm_id: int):  # pylint: disable=unused-argument
    """Remove an alarm from the Sonos system by its ID.

    Args:
        zone (`SoCo`): A SoCo instance, which can be any zone that belongs
            to the Sonos system in which the required alarm is defined.
        alarm_id (str): The ID of the alarm to be removed.

    Returns:
        bool: `True` if the alarm is found and removed, `False` otherwise.
    """
    log.warning(
        "remove_alarm_by_id() is deprecated and replaced by `Alarms.remove_by_id()`"
    )
    alarms = Alarms()
    return alarms.remove_by_id(alarm_id)


def parse_alarm_payload(payload: str, zone: SoCo):
    """Parse the XML payload response and return a list of `Alarm` instances."""
    alarm_list = payload["CurrentAlarmList"]
    tree = XML.fromstring(alarm_list.encode("utf-8"))

    # An alarm list looks like this:
    # <Alarms>
    #     <Alarm ID="14" StartTime="07:00:00"
    #         Duration="02:00:00" Recurrence="DAILY" Enabled="1"
    #         RoomUUID="RINCON_000ZZZZZZ1400"
    #         ProgramURI="x-rincon-buzzer:0" ProgramMetaData=""
    #         PlayMode="SHUFFLE_NOREPEAT" Volume="25"
    #         IncludeLinkedZones="0"/>
    #     <Alarm ID="15" StartTime="07:00:00"
    #         Duration="02:00:00" Recurrence="DAILY" Enabled="1"
    #         RoomUUID="RINCON_000ZZZZZZ01400"
    #         ProgramURI="x-rincon-buzzer:0" ProgramMetaData=""
    #         PlayMode="SHUFFLE_NOREPEAT" Volume="25"
    #          IncludeLinkedZones="0"/>
    # </Alarms>

    alarms = tree.findall("Alarm")
    result = []
    for alarm in alarms:
        values = alarm.attrib
        alarm_id = int(values["ID"])

        args = {}
        args["zone"] = next(
            (z for z in zone.all_zones if z.uid == values["RoomUUID"]), None
        )
        if args["zone"] is None:
            # Some alarms are not associated to a zone, ignore these
            continue

        args["start_time"] = datetime.strptime(
            values["StartTime"], "%H:%M:%S"
        ).time()  # StartTime not StartLocalTime which is used by CreateAlarm
        args["duration"] = (
            None
            if values["Duration"] == ""
            else datetime.strptime(values["Duration"], "%H:%M:%S").time()
        )
        args["recurrence"] = values["Recurrence"]
        args["enabled"] = values["Enabled"] == "1"
        args["program_uri"] = (
            None
            if values["ProgramURI"] == "x-rincon-buzzer:0"
            else values["ProgramURI"]
        )
        args["program_metadata"] = values["ProgramMetaData"]
        args["play_mode"] = values["PlayMode"]
        args["volume"] = values["Volume"]
        args["include_linked_zones"] = values["IncludeLinkedZones"] == "1"

        new_alarm = Alarm(**args)
        new_alarm.alarm_id = alarm_id

        result.append(new_alarm)
    return result
