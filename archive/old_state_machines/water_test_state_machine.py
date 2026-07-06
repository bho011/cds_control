import time

from models.process_state import ProcessState


class WaterTestStateMachine:
    def __init__(
        self,
        mixer_refill_pump,
        supply_valve,
        drain_valve,
        run_seconds: float = 5.0,
        valve_settle_seconds: float = 1.0
    ):
        self.state = ProcessState.IDLE

        self.mixer_refill_pump = mixer_refill_pump
        self.supply_valve = supply_valve
        self.drain_valve = drain_valve

        self.run_seconds = run_seconds
        self.valve_settle_seconds = valve_settle_seconds

        self.state_started_at = time.monotonic()
        self.error_message = None

    def start(self):
        if self.state != ProcessState.IDLE:
            print("[WARN] Prozess wurde bereits gestartet.")
            return

        self._change_state(ProcessState.OPEN_VALVES)

    def update(self):
        if self.state == ProcessState.IDLE:
            return

        if self.state == ProcessState.OPEN_VALVES:
            self._handle_open_valves()

        elif self.state == ProcessState.START_PUMP:
            self._handle_start_pump()

        elif self.state == ProcessState.RUNNING:
            self._handle_running()

        elif self.state == ProcessState.STOP_PUMP:
            self._handle_stop_pump()

        elif self.state == ProcessState.CLOSE_VALVES:
            self._handle_close_valves()

        elif self.state == ProcessState.FINISHED:
            return

        elif self.state == ProcessState.ERROR:
            self.safe_shutdown()

    def _handle_open_valves(self):
        self.supply_valve.on()
        self.drain_valve.on()

        if self._state_elapsed_seconds() >= self.valve_settle_seconds:
            self._change_state(ProcessState.START_PUMP)

    def _handle_start_pump(self):
        self.mixer_refill_pump.on()
        self._change_state(ProcessState.RUNNING)

    def _handle_running(self):
        elapsed = self._state_elapsed_seconds()
        remaining = max(0, self.run_seconds - elapsed)

        print(f"[RUNNING] Restzeit: {remaining:.1f} Sekunden")

        if elapsed >= self.run_seconds:
            self._change_state(ProcessState.STOP_PUMP)

    def _handle_stop_pump(self):
        self.mixer_refill_pump.off()
        self._change_state(ProcessState.CLOSE_VALVES)

    def _handle_close_valves(self):
        self.supply_valve.off()
        self.drain_valve.off()
        self._change_state(ProcessState.FINISHED)

    def error(self, message: str):
        self.error_message = message
        print(f"[ERROR] {message}")
        self._change_state(ProcessState.ERROR)

    def safe_shutdown(self):
        print("[SAFE] Sicherer Stopp wird ausgeführt.")
        self.mixer_refill_pump.off()
        self.supply_valve.off()
        self.drain_valve.off()

    def _change_state(self, new_state: ProcessState):
        print(f"[STATE] {self.state.name} -> {new_state.name}")
        self.state = new_state
        self.state_started_at = time.monotonic()

    def _state_elapsed_seconds(self) -> float:
        return time.monotonic() - self.state_started_at

    @property
    def is_done(self) -> bool:
        return self.state in [ProcessState.FINISHED, ProcessState.ERROR]