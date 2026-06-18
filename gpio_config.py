# gpio_config.py

OUTPUTS = {
    "contactor_0": 12,
    "mixer_refill_pump": 20, #phys. Contactor 1 / GPIO20 / PIN 38,
    "contactor_2": 22,
    "contactor_3": 23,
    "contactor_4": 26,
    "contactor_5": 13,

    "valve_0_drain": 21,
    "valve_1": 27, # valve 1 / GPIO 27 / PIN 13 
    "valve_2": 19,
    "valve_3": 24,
    "valve_4": 25,
    "valve_5": 5, # not reliably waterproof/ nicht zuverlässig dicht 
    "test_supply_valve_6": 6, #valve 6 / GPIO 6 / PIN31 - ROWatter comes from mixer_refill_pump from here for Test,
    "valve_7": 16,
    "valve_8": 17,
    "valve_9": 18,
}

# Relaiskarten sind häufig active-low.
# True bedeutet:
# GPIO LOW  = Ausgang EIN
# GPIO HIGH = Ausgang AUS
ACTIVE_LOW = True

PULSE_SECONDS = 180.0
