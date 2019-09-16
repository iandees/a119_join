import argparse
import datetime
import json
import os
import piexif
import pytz
import subprocess
import tempfile
import nvtk_mp42gpx
from fractions import Fraction
from haversine import haversine

def coord_to_rational(coord_deg, loc):
    """convert decimal coordinates into degrees, munutes and seconds tuple
    Keyword arguments:
        coord_deg is float gps-value as decimal degrees
        loc is direction list ["S", "N"] or ["W", "E"]
    return:
        tuple like (25, 13, 48.343 ,'N')
    """
    if coord_deg < 0:
        loc_value = loc[0]
    elif coord_deg > 0:
        loc_value = loc[1]
    else:
        loc_value = ""

    abs_value = abs(coord_deg)
    deg = int(abs_value)
    t1 = (abs_value-deg)*60
    min = int(t1)
    sec = round((t1 - min)* 60, 5)

    return (deg, min, sec, loc_value)

def float_to_rational(number):
    """convert a number to rational
    Keyword arguments:
        number
    return:
        tuple like (1, 2), (numerator, denominator)
    """
    f = Fraction(str(number))
    return (f.numerator, f.denominator)

def set_gps_location(file_name, time, lat, lng, altitude, bearing):
    """Adds GPS position as EXIF metadata
    Keyword arguments:
        file_name -- image file name
        lat -- latitude (as float)
        lng -- longitude (as float)
        altitude -- altitude (as float)
        bearing -- bearing (as float)
    """
    lat_deg = coord_to_rational(lat, ["S", "N"])
    lng_deg = coord_to_rational(lng, ["W", "E"])

    exiv_lat = (float_to_rational(lat_deg[0]), float_to_rational(lat_deg[1]), float_to_rational(lat_deg[2]))
    exiv_lng = (float_to_rational(lng_deg[0]), float_to_rational(lng_deg[1]), float_to_rational(lng_deg[2]))

    gps_ifd = {
        piexif.GPSIFD.GPSVersionID: (2, 0, 0, 0),
        piexif.GPSIFD.GPSLatitudeRef: lat_deg[3],
        piexif.GPSIFD.GPSLatitude: exiv_lat,
        piexif.GPSIFD.GPSLongitudeRef: lng_deg[3],
        piexif.GPSIFD.GPSLongitude: exiv_lng,
    }

    if bearing != 0.0:
        gps_ifd[piexif.GPSIFD.GPSImgDirection] = (int((bearing % 360.0) * 100), 100)
        gps_ifd[piexif.GPSIFD.GPSImgDirectionRef] = 'T'

    exif_ifd = {
        piexif.ExifIFD.DateTimeOriginal: time.astimezone(pytz.utc).strftime('%Y:%m:%d %H:%M:%S.%f')[:-3],
    }

    exif_dict = {"Exif": exif_ifd, "GPS": gps_ifd}
    exif_bytes = piexif.dump(exif_dict)
    piexif.insert(exif_bytes, file_name)

def lerp(from_val, to_val, ratio):
    delta_val = (to_val - from_val)
    return to_val + (delta_val * ratio)

def lerp_point(pt1, pt2, ratio):
    # Special case time because it's not a single number
    dt = (pt2.time - pt1.time).total_seconds()
    new_time = pt1.time + datetime.timedelta(seconds=(dt * ratio))

    new_point = nvtk_mp42gpx.GpsPoint(
        time=new_time,
        lat=lerp(pt1.lat, pt2.lat, ratio),
        lon=lerp(pt1.lon, pt2.lon, ratio),
        speed=lerp(pt1.speed, pt2.speed, ratio),
        bearing=lerp(pt1.bearing, pt2.bearing, ratio),
    )

    return new_point

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('filenames', nargs='+')
    parser.add_argument('--output', default='.', help='The output directory where the geotagged photos end up.')
    parser.add_argument('--tz', default='America/Chicago', help='The timezone the camera was set in.')
    parser.add_argument('--fps', type=int, default=1, help='The number of frames per second to extract from the video.')
    args = parser.parse_args()

    timezone = pytz.timezone(args.tz)
    assert args.fps > 0, "Need to have 1+ fps"

    for video_file in args.filenames:
        print("Extracting GPS data from %s..." % video_file)
        gps_data = nvtk_mp42gpx.extract_gpx(video_file, tz=timezone)

        with tempfile.TemporaryDirectory() as temp_dir:
            thumb_format = os.path.join(temp_dir, 'thumb_%d.jpg')

            print("Generating frames from %s..." % video_file)
            subprocess.check_output(['ffmpeg', '-i', video_file, '-vf', 'fps=%d' % args.fps, thumb_format, '-hide_banner'], stderr=subprocess.DEVNULL)

            current_frame = 1
            prev_gps_point = None

            print("Applying GPS EXIF data to frames...")
            for gps_index, gps_point in enumerate(gps_data):
                if not gps_point:
                    current_frame += args.fps
                    continue

                if not prev_gps_point:
                    prev_gps_point = gps_point

                for interpolation_step in range(args.fps):
                    image_name = thumb_format % current_frame
                    ratio = (interpolation_step / args.fps)
                    interpolated_point = lerp_point(prev_gps_point, gps_point, ratio)

                    set_gps_location(
                        image_name,
                        interpolated_point.time,
                        interpolated_point.lat,
                        interpolated_point.lon,
                        0,
                        interpolated_point.bearing,
                    )

                    frame_timestamp = interpolated_point.time.strftime('%Y-%m-%d-%H-%M-%S-%f')
                    output_filename = 'frame-%s.jpg' % frame_timestamp
                    os.rename(image_name, os.path.join(args.output, output_filename))

                    current_frame += 1

                prev_gps_point = gps_point

    print("Done.")

if __name__ == "__main__":
    main()
