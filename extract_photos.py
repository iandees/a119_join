import argparse
import datetime
import ephem
import haversine
import json
import os
import piexif
import pytz
import subprocess
import tempfile
import nvtk_mp42gpx
from fractions import Fraction

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

    # Special case for bearing because 0° and 359° are very close
    # From https://stackoverflow.com/a/14498790/73004
    shortest_angle = ((pt1.bearing - pt2.bearing) + 180) % 360 - 180
    new_bearing = pt1.bearing + (shortest_angle * ratio) % 360

    new_point = nvtk_mp42gpx.GpsPoint(
        time=new_time,
        lat=lerp(pt1.lat, pt2.lat, ratio),
        lon=lerp(pt1.lon, pt2.lon, ratio),
        speed=lerp(pt1.speed, pt2.speed, ratio),
        bearing=new_bearing,
    )

    return new_point

def ignore_frame(ignored_points, point):
    if point.speed < 4:
        return True

    p2 = (point.lat, point.lon)

    for ignored in ignored_points:
        lat, lon, radius = ignored
        dist = haversine.haversine((lat, lon), p2, haversine.Unit.METERS)
        if dist < radius:
            return True

    return False

def is_light_out(point):
    sun = ephem.Sun()

    obs = ephem.Observer()
    obs.lat = '%0.5f' % point.lat
    obs.lon = '%0.5f' % point.lon
    obs.date = point.time.astimezone(pytz.utc).strftime('%Y/%m/%d %H:%M')

    sun.compute(obs)

    twilight = -2 * ephem.degree
    return sun.alt > twilight

def has_movement(points):
    # Filter out points that don't have GPS signal
    pts = filter(None, points)
    # ... and where speed is very low
    pts = filter(lambda p: p.speed > 0.5, pts)

    return any(pts)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('filenames', nargs='+')
    parser.add_argument('--output', default='.', help='The output directory where the geotagged photos end up.')
    parser.add_argument('--tz', default='America/Chicago', help='The timezone the camera was set in.')
    parser.add_argument('--fps', type=int, default=1, help='The number of frames per second to extract from the video.')
    parser.add_argument('--ignore-point', action='append', help='Specify a lat,lon,radius to not output frames for.')
    args = parser.parse_args()

    tz = pytz.timezone(args.tz)
    assert args.fps > 0, "Need to have 1+ fps"

    ignore_points = []
    for p in args.ignore_point:
        split_str = p.split(',')
        assert len(split_str) == 3, "Ignore points should be in the format lat,lon,radius"
        ignore_point = tuple(map(float, split_str))
        ignore_points.append(ignore_point)

    for video_file in args.filenames:
        print("Extracting GPS data from %s..." % video_file)
        gps_data = nvtk_mp42gpx.extract_gpx(video_file, tz=tz)

        # Find the last GPS point in the file
        latest_point = None
        for g in filter(None, gps_data):
            if latest_point is None or g.time > latest_point.time:
                latest_point = g

        if not latest_point:
            print("File %s has no GPS data, so skipping it" % (video_file,))
            continue

        if not is_light_out(latest_point):
            print("File %s ends when the sun is down, so skipping it" % (video_file,))
            continue

        if not has_movement(gps_data):
            print("File %s does not include any movement, so skipping it" % (video_file,))
            continue

        with tempfile.TemporaryDirectory() as temp_dir:
            thumb_format = os.path.join(temp_dir, 'thumb_%d.jpg')

            print("Generating frames from %s..." % video_file)
            subprocess.check_output(['ffmpeg', '-i', video_file, '-qscale:v', '1', '-qmin', '1', '-qmax', '1', '-vf', 'fps=%d' % args.fps, thumb_format, '-hide_banner'], stderr=subprocess.DEVNULL)

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

                    if not ignore_frame(ignore_points, interpolated_point):
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
