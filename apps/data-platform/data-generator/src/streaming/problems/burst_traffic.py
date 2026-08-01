class BurstTrafficProblem:
    def __init__(self, every_n_ticks: int, multiplier: int):
        self.every_n_ticks = every_n_ticks
        self.multiplier = multiplier

    def events_for_tick(self, tick: int, normal_events: int) -> int:
        if self.every_n_ticks > 0 and tick % self.every_n_ticks == 0:
            return normal_events * self.multiplier
        return normal_events
