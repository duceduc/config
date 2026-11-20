"""The kinds of workout a record can name, and the key each one is shown under.

The Android app sends Health Connect's exercise type constant as a string of digits
(`"56"` for running); the iOS app sends HealthKit's activity as a snake_case name
(`"running"`). Both end up as one of the keys below, which are the states of the Last
workout sensor and the translation keys under `entity.sensor.last_workout.state`.

The numbers are `ExerciseSessionRecord.EXERCISE_TYPE_*` from androidx.health.connect;
the names are the iOS app's `HKWorkoutActivityType.name`. Anything not listed, a type a
newer Health Connect or iOS added, reads as "other", and the sensor's `type_code`
attribute still holds what was sent.

This module deliberately imports nothing from Home Assistant, and nothing else of this
integration, so payload.py can take the list of keys for the sensor's options.
"""

from __future__ import annotations

from typing import Any, Final

OTHER: Final = "other"

# Health Connect's exercise type constants. Every one of them has a key of its own.
HEALTH_CONNECT_TYPES: Final[dict[str, str]] = {
    "0": OTHER,
    "2": "badminton",
    "4": "baseball",
    "5": "basketball",
    "8": "biking",
    "9": "biking_stationary",
    "10": "boot_camp",
    "11": "boxing",
    "13": "calisthenics",
    "14": "cricket",
    "16": "dancing",
    "25": "elliptical",
    "26": "exercise_class",
    "27": "fencing",
    "28": "american_football",
    "29": "australian_football",
    "31": "frisbee_disc",
    "32": "golf",
    "33": "guided_breathing",
    "34": "gymnastics",
    "35": "handball",
    "36": "hiit",
    "37": "hiking",
    "38": "ice_hockey",
    "39": "ice_skating",
    "44": "martial_arts",
    "46": "paddling",
    "47": "paragliding",
    "48": "pilates",
    "50": "racquetball",
    "51": "rock_climbing",
    "52": "roller_hockey",
    "53": "rowing",
    "54": "rowing_machine",
    "55": "rugby",
    "56": "running",
    "57": "running_treadmill",
    "58": "sailing",
    "59": "scuba_diving",
    "60": "skating",
    "61": "skiing",
    "62": "snowboarding",
    "63": "snowshoeing",
    "64": "soccer",
    "65": "softball",
    "66": "squash",
    "68": "stair_climbing",
    "69": "stair_climbing_machine",
    "70": "strength_training",
    "71": "stretching",
    "72": "surfing",
    "73": "swimming_open_water",
    "74": "swimming_pool",
    "75": "table_tennis",
    "76": "tennis",
    "78": "volleyball",
    "79": "walking",
    "80": "water_polo",
    "81": "weightlifting",
    "82": "wheelchair",
    "83": "yoga",
}

# HealthKit's activities that have a Health Connect counterpart. HealthKit's "swimming"
# does not say pool or open water, so it keeps a key of its own. The activities that
# match nothing here (archery, curling, tai chi, ...) read as "other".
_IOS_TYPES: Final[dict[str, str]] = {
    "american_football": "american_football",
    "australian_football": "australian_football",
    "badminton": "badminton",
    "baseball": "baseball",
    "basketball": "basketball",
    "boxing": "boxing",
    "climbing": "rock_climbing",
    "core_training": "calisthenics",
    "cricket": "cricket",
    "cycling": "biking",
    "dance": "dancing",
    "dance_inspired_training": "dancing",
    "cardio_dance": "dancing",
    "social_dance": "dancing",
    "disc_sports": "frisbee_disc",
    "elliptical": "elliptical",
    "fencing": "fencing",
    "flexibility": "stretching",
    "functional_strength_training": "strength_training",
    "golf": "golf",
    "gymnastics": "gymnastics",
    "hand_cycling": "biking",
    "handball": "handball",
    "hiit": "hiit",
    "hiking": "hiking",
    "kickboxing": "martial_arts",
    "martial_arts": "martial_arts",
    "paddle_sports": "paddling",
    "pilates": "pilates",
    "racquetball": "racquetball",
    "rowing": "rowing",
    "rugby": "rugby",
    "running": "running",
    "sailing": "sailing",
    "skating_sports": "skating",
    "cross_country_skiing": "skiing",
    "downhill_skiing": "skiing",
    "snowboarding": "snowboarding",
    "soccer": "soccer",
    "softball": "softball",
    "squash": "squash",
    "stair_climbing": "stair_climbing",
    "stairs": "stair_climbing_machine",
    "surfing_sports": "surfing",
    "swimming": "swimming",
    "table_tennis": "table_tennis",
    "tennis": "tennis",
    "traditional_strength_training": "strength_training",
    "underwater_diving": "scuba_diving",
    "volleyball": "volleyball",
    "walking": "walking",
    "water_polo": "water_polo",
    "wheelchair_run_pace": "wheelchair",
    "wheelchair_walk_pace": "wheelchair",
    "wrestling": "martial_arts",
    "yoga": "yoga",
}

# Health Connect's exercise segment type constants (`ExerciseSegment.EXERCISE_SEGMENT_TYPE_*`
# from androidx.health.connect), the exercise or set inside a session: `"51"` is a squat.
# A segment the phone sends as a number nothing here knows reads as "other".
HEALTH_CONNECT_SEGMENTS: Final[dict[str, str]] = {
    "0": "other",
    "1": "arm_curl",
    "2": "back_extension",
    "3": "ball_slam",
    "4": "barbell_shoulder_press",
    "5": "bench_press",
    "6": "bench_sit_up",
    "7": "biking",
    "8": "biking_stationary",
    "9": "burpee",
    "10": "crunch",
    "11": "deadlift",
    "12": "double_arm_triceps_extension",
    "13": "dumbbell_curl_left_arm",
    "14": "dumbbell_curl_right_arm",
    "15": "dumbbell_front_raise",
    "16": "dumbbell_lateral_raise",
    "17": "dumbbell_row",
    "18": "dumbbell_triceps_extension_left_arm",
    "19": "dumbbell_triceps_extension_right_arm",
    "20": "dumbbell_triceps_extension_two_arm",
    "21": "elliptical",
    "22": "forward_twist",
    "23": "front_raise",
    "24": "high_intensity_interval_training",
    "25": "hip_thrust",
    "26": "hula_hoop",
    "27": "jumping_jack",
    "28": "jump_rope",
    "29": "kettlebell_swing",
    "30": "lateral_raise",
    "31": "lat_pull_down",
    "32": "leg_curl",
    "33": "leg_extension",
    "34": "leg_press",
    "35": "leg_raise",
    "36": "lunge",
    "37": "mountain_climber",
    "38": "other",
    "39": "pause",
    "40": "pilates",
    "41": "plank",
    "42": "pull_up",
    "43": "punch",
    "44": "rest",
    "45": "rowing_machine",
    "46": "running",
    "47": "running_treadmill",
    "48": "shoulder_press",
    "49": "single_arm_triceps_extension",
    "50": "sit_up",
    "51": "squat",
    "52": "stair_climbing",
    "53": "stair_climbing_machine",
    "54": "stretching",
    "55": "swimming_backstroke",
    "56": "swimming_breaststroke",
    "57": "swimming_butterfly",
    "58": "swimming_freestyle",
    "59": "swimming_mixed",
    "60": "swimming_open_water",
    "61": "swimming_other",
    "62": "swimming_pool",
    "63": "upper_twist",
    "64": "walking",
    "65": "weightlifting",
    "66": "wheelchair",
    "67": "yoga",
}


#: Every key a workout can be shown as, which is the options of the Last workout sensor.
WORKOUT_TYPES: Final[tuple[str, ...]] = tuple(
    dict.fromkeys([*HEALTH_CONNECT_TYPES.values(), *_IOS_TYPES.values()])
)


def type_code(raw: Any) -> str:
    """The type as the phone sent it, or "" when it sent nothing usable.

    An integer is taken as its digits, in case an app version sends the constant as a
    number; true and false are no type.
    """
    if isinstance(raw, bool):
        return ""
    if isinstance(raw, int):
        return str(raw)
    return raw.strip() if isinstance(raw, str) else ""


def workout_type(raw: Any) -> str:
    """The key a record's `type` is shown as; "other" for a type nothing here knows."""
    code = type_code(raw)
    return HEALTH_CONNECT_TYPES.get(code) or _IOS_TYPES.get(code.lower(), OTHER)


def segment_type(raw: Any) -> str:
    """The key an exercise segment's `type` is shown as; "other" for one nothing here knows."""
    return HEALTH_CONNECT_SEGMENTS.get(type_code(raw), OTHER)
