
#  https://community.home-assistant.io/t/light-fade-in/35509/40

# calling the script
#- service: python_script.lightfader
#  data: 
#    entity_id: light.bedroom_lights
#    transition: 20
#    brightness: 100


entity_id = data.get('entity_id')
transition_time = int(data.get('transition'))
brightness_pct = int(data.get('brightness'))

# Hard coded values
MIN_TIME_BETWEEN_CALLS = 0.5
MIN_STEP_SIZE = 1
MAX_BRIGHTNESS_DEVIATION = 3

# Helper functions
def calc_delta(new_value, old_value):
    return abs(abs(new_value) - abs(old_value))

# Absolute values used in calculations for API-Calls
initial_brightness = int(hass.states.get(entity_id).attributes.get('brightness') or 0)
final_brightness = int(255 * brightness_pct / 100)

# Time Setup
start_time = time.time()
end_time = start_time + transition_time
transition_range = final_brightness - initial_brightness

# Variable used for rate limiting
previous_passed_time = 0
previous_step_over_start = 0

# Variables used to determine user triggered stop
previous_level = initial_brightness
user_changed_brightness = False

# Transition loop
while end_time > time.time():
    # Calculate current brightness based on current time
    passed_time = transition_time - (end_time - time.time())
    progress_ratio = passed_time / transition_time

    step_over_start = transition_range * progress_ratio
    new_level = int(initial_brightness + step_over_start)

    # This check makes sure the light bulb cannot be called more than twice per second and the individual increases need to be the size of at least 1
    if calc_delta(passed_time, previous_passed_time) > MIN_TIME_BETWEEN_CALLS and \
       calc_delta(step_over_start, previous_step_over_start) > MIN_STEP_SIZE:
        # This check causes the transition to stop if the brightness was changed by another script/user
        # I put it in here to avoid quering the current value of the light too many times
        actual_brightness = int(hass.states.get(entity_id).attributes.get('brightness') or 0)
        if calc_delta(actual_brightness, previous_level) < MAX_BRIGHTNESS_DEVIATION and \
           calc_delta(previous_level, actual_brightness) < MAX_BRIGHTNESS_DEVIATION:
            data = {"entity_id": entity_id, 'brightness': new_level}
            hass.services.call('light', 'turn_on', data)
            previous_passed_time = passed_time
            previous_step_over_start = step_over_start
            previous_level = new_level
        else:
            user_changed_brightness = True
            break

# Set to light to end terminal brightness
if not user_changed_brightness:
    data = {"entity_id": entity_id, 'brightness': final_brightness}
    hass.services.call('light', 'turn_on', data)