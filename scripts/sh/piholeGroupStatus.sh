#!/bin/bash

# requires jq
# uses the PiHole 6 API to enable / disable and check the status of a group.

# usage:  piholeGroupStatus.sh PiHoleAPIAddress PiHoleAPIPWD GroupName Action
# Action can be enable / disable / status

# How to use in Home Assistant

# command_line:
#   - switch:
#       name: PiHoleSwitchName
#       command_on: ./piholeGroupStatus.sh http://pi.hole:80/api/ API_PWD GroupName enable
#       command_off: ./piholeGroupStatus.sh http://pi.hole:80/api/ API_PWD GroupName disable
#       command_state: ./piholeGroupStatus.sh http://pi.hole:80/api/ API_PWD GroupName status

PiHoleAPI=$1
PiHolePWD=$2
GroupName=$3
Action=$4

#global variables
sid=0  #sessionID
GroupComment=""
GroupStatus=""

openSession(){
    response=`curl -s -X POST "$PiHoleAPI/auth" -d '{"password":"'$PiHolePWD'"}' `
    sid=`echo $response | jq -r .session.sid`
}

closeSession(){
    curl -X DELETE "$PiHoleAPI/auth"  -H "accept: application/json" -H "sid: $sid"
}

getGroupDetails(){
    GroupDetails=`curl --silent -X GET "$PiHoleAPI/groups/$GroupName" \
        -H "accept: application/json" \
        -H "sid: $sid" `

    GroupComment=`echo $GroupDetails | jq -r .groups[0].comment`
    GroupStatus=`echo $GroupDetails | jq -r .groups[0].enabled`
}

enableGroup(){
    local Payload=$( jq -n \
                  --arg gn "$GroupName" \
                  --arg gc "$GroupComment" \
                  --argjson en true \
                  '{name: $gn, comment: $gc, enabled: $en}' )

    response=`curl -s -X PUT "$PiHoleAPI/groups/$GroupName" \
        -H "accept: application/json" \
        -H "sid: $sid" \
        -d  "$Payload" `
}

disableGroup(){
    local Payload=$( jq -n \
                  --arg gn "$GroupName" \
                  --arg gc "$GroupComment" \
                  --argjson en false \
                  '{name: $gn, comment: $gc, enabled: $en}' )

    response=`curl -s -X PUT "$PiHoleAPI/groups/$GroupName" \
        -H "accept: application/json" \
        -H "sid: $sid" \
        -d  "$Payload" `
}

case "$Action" in
   "enable")
        openSession
        getGroupDetails
        enableGroup
        closeSession
        exit 0
       ;;
   "disable")
        openSession
        getGroupDetails
        disableGroup
        closeSession
        exit 0
       ;;
   "status")
        openSession
        getGroupDetails
        closeSession

        if [ $GroupStatus = 'true' ]; then
            exit 0
        else
	        exit 1
        fi
       ;;
   *)
   echo "usage:  piholeGroupStatus.sh PiHoleAPIAddress PiHoleAPIPWD GroupName Action"
   exit 1
esac
