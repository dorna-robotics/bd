"""falcon — vision/sensor checks. Empty for the scene-first scaffold."""


class Checks:

    def __init__(self, rcp, rt, **kwargs):
        self.rcp = rcp
        self.rt  = rt

    def register(self, runner) -> None:
        pass
