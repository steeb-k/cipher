from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
from typing import Any, cast

from gi.repository import Gio, GLib

from gsecrets import const

BUS_NAME = "net.reactivated.Fprint"

# For calls that only ask fprintd about itself and answer from memory.
DBUS_TIMEOUT = 500  # In milliseconds

# For calls that drive the sensor. Claim opens the USB device and VerifyStart
# arms it, neither of which is bounded by anything fprintd controls, and half a
# second is not enough on a Goodix MOC reader -- particularly just after resume,
# when the whole bus is still settling.
#
# Timing out here does not cancel anything: fprintd carries on and completes the
# open, while this side has already concluded the claim failed. The two then
# disagree about who owns the device, which is its own route into the wedged
# state the sleep guard below exists to prevent.
DEVICE_TIMEOUT = 15000  # In milliseconds

VERIFY_STATUS_TUPLE = 2

LOGIN1_BUS = "org.freedesktop.login1"
LOGIN1_PATH = "/org/freedesktop/login1"
LOGIN1_MANAGER = "org.freedesktop.login1.Manager"

# fprintd scopes a claim to the D-Bus connection, so it belongs to the process,
# not to any one verifier. _is_claimed below is per instance, and instances are
# replaced whenever the unlock view is rebuilt -- so a verifier can be discarded
# while the process still holds its claim, leaving the replacement convinced
# nothing is claimed and unable to release it. Tracking the holder here is what
# lets a later verifier clean up after an earlier one.
_claim_holder: "FingerprintVerifier | None" = None

# Claiming and releasing must not interleave. A no-match makes the signal
# handler schedule verify_stop() while _on_fingerprint_failure separately
# schedules verify_start(), so the two run as concurrent tasks and race over the
# same device: the stop releases what the start just claimed, or the start
# claims before the stop has finished, and fprintd answers with "not claimed
# before use" or "already claimed". Retries hide it, which is why fingerprint
# unlock worked but only sometimes and only after several attempts.
#
# Module level rather than per instance, because the claim belongs to the
# process and verifier instances are replaced while it is held.
_device_lock = asyncio.Lock()


class _SleepGuard:
    """Keeps the fingerprint device out of the way of system suspend.

    A verify still running when the machine suspends leaves the device open
    inside fprintd with no owning client: fprintd forgets the claim, but
    libfprint never closes the handle. Every later Claim then fails with "the
    device has already been opened", and fingerprint authentication stops
    working for every application on the system -- login, sudo, the screen
    lock -- until fprintd is restarted, which needs root. That is far too much
    damage for a password manager to do to a machine, so the reader is released
    before the system goes down rather than being left armed through it.

    The delay inhibitor is what makes the release reliable. PrepareForSleep is
    broadcast whether or not anyone is listening, and with no lock held the
    system carries on suspending while the release is still in flight -- the
    same race, only narrower. Holding one gives logind's grace period (five
    seconds by default) to finish, and dropping it is what says we are done.

    The lock is held only while the device is actually claimed, so nothing here
    delays a suspend during the overwhelming majority of the time when Cipher is
    not using the sensor at all.
    """

    def __init__(self) -> None:
        self._connection: Gio.DBusConnection | None = None
        self._subscription = 0
        self._fd = -1
        self._verifier: FingerprintVerifier | None = None
        # The verifier to re-arm once the machine comes back, remembered across
        # the suspend because releasing the device necessarily forgets it.
        self._resume_target: FingerprintVerifier | None = None

    async def arm(self, verifier: FingerprintVerifier) -> None:
        """Watch for suspend on behalf of a verifier that holds the device."""
        self._verifier = verifier

        try:
            await self._subscribe()
            await self._take_lock()
        except GLib.Error as err:
            # Best effort throughout. logind may be absent, or may refuse the
            # lock; either way the fingerprint reader must still work, it is
            # just no longer protected against a suspend landing mid-verify.
            logging.debug("Could not guard the fingerprint device on sleep: %s", err)

    def disarm(self) -> None:
        """Stop watching. Called once the device has actually been released."""
        self._verifier = None
        self._drop_lock()

    async def _subscribe(self) -> None:
        if self._subscription:
            return

        self._connection = await Gio.bus_get(Gio.BusType.SYSTEM, None)
        self._subscription = self._connection.signal_subscribe(
            LOGIN1_BUS,
            LOGIN1_MANAGER,
            "PrepareForSleep",
            LOGIN1_PATH,
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_prepare_for_sleep,
            None,
        )

    async def _take_lock(self) -> None:
        if self._fd != -1 or self._connection is None:
            return

        reply, fd_list = await self._connection.call_with_unix_fd_list(
            LOGIN1_BUS,
            LOGIN1_PATH,
            LOGIN1_MANAGER,
            "Inhibit",
            GLib.Variant(
                "(ssss)",
                (
                    "sleep",
                    const.NAME,
                    "Releasing the fingerprint reader",
                    # Delay, never block: this must postpone a suspend for as
                    # long as the release takes and not one moment longer.
                    "delay",
                ),
            ),
            GLib.VariantType("(h)"),
            Gio.DBusCallFlags.NONE,
            DBUS_TIMEOUT,
            None,
            None,
        )

        if fd_list is None:
            return

        self._fd = fd_list.get(reply[0])

    def _drop_lock(self) -> None:
        if self._fd == -1:
            return

        fd, self._fd = self._fd, -1
        try:
            os.close(fd)
        except OSError as err:
            logging.debug("Could not drop the sleep inhibitor: %s", err)

    def _on_prepare_for_sleep(self, *args) -> None:
        # (connection, sender, path, interface, signal, parameters, user_data)
        going_to_sleep = args[5][0]

        app = Gio.Application.get_default()
        if app is None:
            return

        if going_to_sleep:
            app.create_asyncio_task(self._suspend())
        elif self._resume_target is not None:
            app.create_asyncio_task(self._resume())

    async def _suspend(self) -> None:
        verifier, self._verifier = self._verifier, None

        try:
            if verifier is not None:
                logging.debug("Releasing the fingerprint device before suspend")
                await verifier.verify_stop()
        except GLib.Error as err:
            # Never propagate: this runs as a detached task while the machine is
            # going down, so the only thing an exception can achieve is a
            # traceback nobody is in a position to read.
            logging.debug("Could not release the device before suspend: %s", err)
        finally:
            # Unconditionally, and only once the release has been attempted: a
            # lock held past this point delays every suspend by logind's full
            # grace period, and one dropped before it reintroduces the race.
            self._resume_target = verifier
            self._drop_lock()

    async def _resume(self) -> None:
        verifier, self._resume_target = self._resume_target, None

        if verifier is None:
            return

        # Harmless if the unlock view was torn down while the machine was
        # asleep: the verifier is disconnected by then and this returns False.
        logging.debug("Re-arming the fingerprint device after resume")
        await verifier.verify_start()


_sleep_guard = _SleepGuard()


class FingerprintVerifier:
    """Provides easy access to a fingerprint device."""

    def __init__(
        self,
        on_success: Callable,
        on_retry: Callable,
        on_failure: Callable,
    ) -> None:
        """Constructor.

        Might throw an exception if dbus is unavailable, fprintd is missing or
        no device is available.

        Arguments:
        ---------
            on_success: Function to be called on success.
            on_retry: Function to be called if the fingerprint sensor suggests retrying.
            on_failure: Function to be called if the fingerprint did not match or the
                        hardware refused in the process.

        """
        self.on_success = on_success
        self.on_retry = on_retry
        self.on_failure = on_failure

        self._is_claimed = False
        self._device_proxy = None
        self._signal = None

    def connect(self, cb: Gio.Callback) -> None:
        """Initializes the connection to the fingerprint device.

        Shouldn't be called manually unless disconnect was called on the object before.

        Might throw an exception if dbus is unavailable, fprindt is missing
        or no device is available.
        """
        if self._is_claimed:
            return

        async def connect_fingerprint():
            con = await Gio.bus_get(Gio.BusType.SYSTEM, None)

            manager_proxy = await Gio.DBusProxy.new(
                con,
                Gio.DBusProxyFlags.NONE,
                None,
                BUS_NAME,
                "/net/reactivated/Fprint/Manager",
                "net.reactivated.Fprint.Manager",
                None,
            )
            if manager_proxy is None:
                logging.debug("Fprintd not available.")
                return

            try:
                variant = await manager_proxy.call(
                    "GetDefaultDevice",
                    None,
                    Gio.DBusCallFlags.NO_AUTO_START,
                    DBUS_TIMEOUT,
                    None,
                )
                device_str = variant[0]
            except GLib.GError:
                logging.exception("Failed to get default fingerprint device")
                return

            if device_str == "":
                logging.debug("No fingerprint sensor found!")
                return

            self._device_proxy = await Gio.DBusProxy.new(
                con,
                Gio.DBusProxyFlags.NONE,
                None,
                BUS_NAME,
                device_str,
                "net.reactivated.Fprint.Device",
                None,
            )
            if self._device_proxy is None:
                logging.debug("Failed to find the default fingerprint device.")
                return

            self._signal = self._device_proxy.connect("g-signal", self._signal_handler)
            logging.debug("Successfully connected to the fingerprint device.")

            cb()

        app = Gio.Application.get_default()
        app.create_asyncio_task(connect_fingerprint())

    def disconnect(self) -> None:
        """Removes the handlers from the signal.

        Should be called when the class is not needed anymore.
        """
        if self._is_claimed or self._device_proxy is None:
            return

        self._is_claimed = False
        self._device_proxy.disconnect(self._signal)
        self._signal = None
        self._device_proxy = None
        logging.debug("Disconnected from the fingerprint device.")

    async def verify_start(self) -> bool:
        """Starts the fingerprint verification process.

        Returns: True if the fingerprint verification started.

        """
        async with _device_lock:
            return await self._verify_start_locked()

    async def _verify_start_locked(self) -> bool:
        if self._device_proxy is None:
            return False

        global _claim_holder  # noqa: PLW0603

        try:
            await self._claim_device()
        except GLib.Error:
            logging.exception("Failed to claim device")
            return False

        self._is_claimed = True
        _claim_holder = self
        try:
            await self._verify_start()
        except GLib.Error:
            logging.exception("Failed to start fingerprint verification on the device")
            # The claim succeeded even though arming did not, so returning here
            # without releasing strands the device: fprintd holds it for this
            # process, no verify is running to end and free it, and nothing on
            # this side will try again. Give it straight back instead.
            try:
                await self._release_device()
            except GLib.Error as err:
                logging.debug("Could not release after a failed start: %s", err)
            else:
                self._is_claimed = False
                _claim_holder = None
            return False

        await _sleep_guard.arm(self)

        logging.debug("Claimed fingerprint device, verification ongoing...")
        return True

    async def verify_stop(self) -> None:
        """Stops the fingerprint verification process."""
        async with _device_lock:
            await self._verify_stop_locked()

    async def _verify_stop_locked(self) -> None:
        global _claim_holder  # noqa: PLW0603

        # Release whenever the process holds a claim, not only when this
        # instance took it. The claim outlives whichever verifier made it, so
        # checking self._is_claimed alone strands it as soon as the unlock view
        # is rebuilt.
        if not self._is_claimed and _claim_holder is None:
            return

        if self._device_proxy is None:
            return

        try:
            await self._verify_stop()
        except GLib.Error as err:
            # Stopping and releasing are separate concerns, and this one is
            # allowed to fail: NoActionInProgress simply means verification had
            # already ended. Letting it skip the release below is what leaves
            # the claim stranded, which is the whole problem being avoided here.
            logging.debug("Could not stop verification (releasing anyway): %s", err)

        try:
            await self._release_device()
        except GLib.Error as err:
            # "Not claimed before use" means the goal has already been reached
            # by another route -- fprintd dropped the claim itself, which is
            # what it does when a verify dies with the device in a bad state.
            # Retrying cannot improve on that, so treat it as released and fall
            # through. Holding the flags instead leaves this verifier convinced
            # it owns a device fprintd says is free, retrying the release on
            # every attempt and filling the journal with denials it can never
            # satisfy.
            if "not claimed" not in err.message:
                # Anything else may still be transient. Keep the flags set so a
                # later stop retries, rather than believing the device is free
                # while fprintd still has it attributed to this process.
                logging.debug("Exception while releasing fingerprint device: %s", err)
                return

            logging.debug("Fingerprint device was already released by fprintd")

        self._is_claimed = False
        _claim_holder = None
        _sleep_guard.disarm()
        logging.debug("Stopped verification, released fingerprint device.")

    #
    # Internal methods
    #

    async def _call_claim(self) -> None:
        if self._device_proxy:
            await self._device_proxy.call(
                "Claim",
                GLib.Variant("(s)", ("",)),
                Gio.DBusCallFlags.NO_AUTO_START,
                DEVICE_TIMEOUT,
                None,
            )

    async def _claim_device(self) -> None:
        try:
            await self._call_claim()
        except GLib.Error as err:
            if "AlreadyInUse" not in err.message:
                raise

            # fprintd reports two different situations under this one error
            # name, and they need opposite handling:
            #
            #   "Device was already claimed"          -- ours, and reclaimable
            #   "Device already in use by another user" -- someone else's
            #
            # Releasing a claim belonging to another user is not permitted, and
            # attempting it raises the same error again from inside the handler,
            # which is how a simple failure to claim turned into a chained
            # traceback. Leave that case alone and report the original error.
            if "another user" in err.message:
                logging.debug("Fingerprint device is held by another user")
                raise

            # Our own claim that nobody released -- a verifier discarded before
            # its release completed, or an unlock view torn down mid-verify.
            # Drop it and take a fresh one so this heals itself, rather than
            # staying broken until the application restarts.
            logging.debug("Device already claimed by this process; reclaiming")
            try:
                await self._release_device()
                await self._call_claim()
            except GLib.Error:
                # Report what actually went wrong -- the failure to claim --
                # rather than whatever the recovery attempt tripped over.
                logging.debug("Could not reclaim the fingerprint device")
                raise err from None

    async def _release_device(self) -> None:
        if self._device_proxy:
            await self._device_proxy.call(
                "Release",
                None,
                Gio.DBusCallFlags.NO_AUTO_START,
                DEVICE_TIMEOUT,
                None,
            )

    async def _verify_start(self) -> None:
        if self._device_proxy:
            await self._device_proxy.call(
                "VerifyStart",
                GLib.Variant("(s)", ("any",)),
                Gio.DBusCallFlags.NO_AUTO_START,
                DEVICE_TIMEOUT,
                None,
            )

    async def _verify_stop(self) -> None:
        if self._device_proxy:
            await self._device_proxy.call(
                "VerifyStop",
                None,
                Gio.DBusCallFlags.NO_AUTO_START,
                DEVICE_TIMEOUT,
                None,
            )

    retry_results = [
        "verify-retry-scan",
        "verify-swipe-too-short",
        "verify-finger-not-centered",
        "verify-remove-and-retry",
    ]

    def _signal_handler(
        self,
        _proxy: Gio.DBusProxy,
        _sender: str,  # pylint: disable=unused-argument
        signal: str,
        args: tuple[Any],
    ) -> None:
        if signal != "VerifyStatus" or len(args) != VERIFY_STATUS_TUPLE:
            return

        result, done = cast(tuple[str, bool], args)  # see mypy/issues/1178

        if result in self.retry_results and not done:
            self.on_retry()

        if done:
            if result != "verify-disconnected":
                app = Gio.Application.get_default()
                app.create_asyncio_task(self.verify_stop())

            if result == "verify-match":
                self.on_success()
            elif result == "verify-no-match":
                self.on_failure()
            else:
                # Only a genuine no-match is an attempt by the user. Terminal
                # results such as verify-disconnected mean the device went away
                # -- which is exactly what happens when the window is hidden and
                # the device is released. Counting those as failures burns the
                # retry budget without a finger ever touching the sensor.
                logging.debug("Fingerprint verification ended with %s", result)
