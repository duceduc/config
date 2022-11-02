 #!/bin/bash
 # https://community.home-assistant.io/t/record-video-from-esp32-cam-config-with-esphome-in-hassio-works-great/153354/69
 # Records for 10 sec.
 # shell_command.security_cam
rm /config/www/data/logs/security_cam.log;
exec &>>/config/www/data/logs/security_cam.log;

date;
echo -e;

echo -e "access token is: $1";
echo -e;

cam_token=$1;

video=/config/www/img/security/cam_record;
img=/config/www/img/security;
id=$(date +"%y-%m-%d_%H-%M-%S")genkan_camera;

http_url=http://192.168.1.20:8123/api/camera_proxy_stream/camera.security_mdf?token=$1;

ffmpeg -i $http_url -t 10 -vcodec copy $video/$id.mp4;

find $video -type f -name '*.mp4' -mtime +30 -exec rm -- '{}' \;
find $img -type f -name '*.jpg' -mtime +30 -exec rm -- '{}' \;

