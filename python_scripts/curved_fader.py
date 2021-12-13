#   curved_fader.py
#   By Ingrid Bakker studioilb.nl
#
#--------------------------------------
#
#  HOW TO CALL
#   Either brightness_end or temperature_end should be defined, otherwise nothing will happen.
#   service: python_script.curved_fader
#   data:
#     entity_id: light.entity
#     brightness_start: 0-255 ; default: current
#     brightness_end: 0-255 ; default: current
#     brightness_curve: 'linear' or 'exp2' or 'exp5' or 'smooth' ; default: 'exp5'
#     temperature_start: 154-370 ; default: current
#     temperature_end: 154-370 ; default: current
#     temperature_curve: 'linear' or 'exp2' or 'exp5' or 'smooth' ; default: 'linear'
#     duration: '00:00:00'
#
#  NOTES
#   adapted from https://community.home-assistant.io/t/light-fader-by-transition-time/99600
#   adapted from https://stackoverflow.com/questions/978599/equation-to-calculate-different-speeds-for-smooth-animation
#   direction is eiter 1 or -1
#
#   FUNCTION: linear
#   round(start+direction*((x-1)*(difference/steps)+1))
#   50% effect a 50% of time
#
#   FUNCTION: exp2
#   n = 2
#   round(abs((direction*start)+(((difference^(1/n)-1)/(steps-1))*(x-1-1)+1)^n))
#   50% effect at 69% time
#
#   FUNCTION: exp5
#   n = 5
#   round(abs((direction*start)+(((difference^(1/n)-1)/(steps-1))*(x-1-1)+1)^n))
#   50% effect at 81% time
#
#   FUNCTION: smooth
#   round(abs(direction*start+difference^(x/steps)))
#   50% effect at 88% time

entity_id = data.get('entity_id')
states = hass.states.get(entity_id)

b_cur = states.attributes.get('brightness') or 0
b_start = round(int(data.get('brightness_start', b_cur)))
b_end = round(int(data.get('brightness_end', b_cur)))
b_curve = data.get('brightness_curve', 'exp5')
b_dif = abs(b_end-b_start)
b_dir = t_dir = 1
if (b_end < b_start): b_dir = -1

t_cur = states.attributes.get('color_temp') or 0
t_start = round(int(data.get('temperature_start',t_cur)))
t_end = round(int(data.get('temperature_end',t_cur)))
t_curve = data.get('temperature_curve', 'linear')
t_dif = abs(t_end-t_start)
if (t_end < t_start): t_dir = -1

x=0
steps = max(b_dif, t_dif)
duration = data.get('duration')
transition = (int(duration[:2]) * 3600 + int(duration[3:5]) * 60 + int(duration[-2:]))
if (steps<1): steps = 1  # when no change is detected: steps = 0, prevent error devide by zero
step_time = abs(transition/steps)

curve = {}
curve['linear'] = lambda x, start, end, dif, dir, steps : round(start+dir*((x-1)*(dif/steps)+1))
curve['exp2'] = lambda x, start, end, dif, dir, steps : round(abs((dir*start)+(((dif**(1/2)-1)/(steps-1))*(x-1-1)+1)**2))
curve['exp5'] = lambda x, start, end, dif, dir, steps : round(abs((dir*start)+(((dif**(1/5)-1)/(steps-1))*(x-1-1)+1)**5))
curve['smooth'] = lambda x, start, end, dif, dir, steps : round(abs(dir*start+dif**(x/steps)))

b_curve = curve[b_curve]
t_curve = curve[t_curve]
if(b_start<1):b_start=1
if(t_start<1):t_start=1
b_new = b_last = b_start
t_new = t_last = t_start

data = { "entity_id" : entity_id, "brightness" : b_new, "color_temp" : t_new }
hass.services.call('light', 'turn_on', data)

time.sleep(0.05) # seconds. Without delay 'hass.states.get' will not be updated yet 

while (b_new != b_end) or (t_new != t_end) :  
  states = hass.states.get(entity_id)
  b_cur = states.attributes.get('brightness') or 0
  t_cur = states.attributes.get('color_temp') or 0 
    
#  For some reasone brightness under 25 is not registerd as an state, but it is visible in my 
#  light. So I do use it, but exclude it from this break. If you want to break the script, just 
#  elevate the brightness above 25, than it will register.
#  step_time of 0.05 sec, in 25 steps corresponds to a max wait of 1,25 seconds

  if ((b_cur > 24) and (step_time > 0.05) and ((b_cur != b_last) or (t_cur != t_last))): 
    # external change, so break
    logger.info('Smooth fader stopped by external change')
    break
    
  x = x + 1
  b_new = b_curve(x, b_start, b_end, b_dif, b_dir, steps)
  t_new = t_curve(x, t_start, t_end, t_dif, t_dir, steps)
    
  if ((b_new != b_last) or (t_new != t_last)):
    data = { "entity_id" : entity_id, "brightness" : b_new, "color_temp" : t_new }
    hass.services.call('light', 'turn_on', data)
    #if (x % 10 == 0): logger.info("Setting %s, brightness from %s to %s and color from %s to %s", entity_id, b_last, b_new, t_last, t_new) 
    b_last = b_new
    t_last = t_new
  time.sleep(step_time)