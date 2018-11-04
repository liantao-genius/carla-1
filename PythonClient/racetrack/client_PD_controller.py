#!/usr/bin/env python3

# Copyright (c) 2017 Computer Vision Center (CVC) at the Universitat Autonoma de
# Barcelona (UAB).
#
# This work is licensed under the terms of the MIT license.
# For a copy, see <https://opensource.org/licenses/MIT>.

"""Basic CARLA client example."""

from __future__ import print_function

import argparse
import logging
import random
import time
import pandas as pd
import numpy as np
from scipy import spatial
from scipy.interpolate import splprep, splev
import matplotlib.pyplot as plt

import sys
sys.path.append('..')
from carla.client import make_carla_client
from carla.sensor import Camera, Lidar
from carla.settings import CarlaSettings
from carla.tcp import TCPConnectionError
from carla.util import print_over_same_line


norm = np.linalg.norm


def run_carla_client(args):
    number_of_episodes = 10
    frames_per_episode = 10000
    spline_points = 10000

    track_DF = pd.read_csv('racetrack.txt', header=None)
    # The track data are rescaled by 100x with relation to Carla measurements
    track_DF = track_DF / 100

    pts_2D = track_DF.loc[:, [0, 1]].values
    tck, u = splprep(pts_2D.T, u=None, s=2.0, per=1, k=3)
    u_new = np.linspace(u.min(), u.max(), spline_points)
    x_new, y_new = splev(u_new, tck, der=0)
    pts_2D = np.c_[x_new, y_new]

    prev_speed = np.nan
    prev_prev_speed = np.nan
    curr_speed = np.nan

    prev_prop = np.nan
    prev_prev_prop = np.nan
    curr_prop = np.nan
    deriv_list = []
    deriv_len = 5

    steer = 0
    throttle = 0.5
    target_speed = args.target_speed

    kp = args.kp
    kd = args.kd

    depth_array = None

    # We assume the CARLA server is already waiting for a client to connect at
    # host:port. To create a connection we can use the `make_carla_client`
    # context manager, it creates a CARLA client object and starts the
    # connection. It will throw an exception if something goes wrong. The
    # context manager makes sure the connection is always cleaned up on exit.
    with make_carla_client(args.host, args.port) as client:
        print('CarlaClient connected')
        for episode in range(0, number_of_episodes):
            # Start a new episode.
            storage = np.random.rand(150, 200, frames_per_episode).astype(np.float16)
            stream = open('log{}.txt'.format(episode), 'w')
            stream.write('frame,steer,throttle,speed\n')

            if args.settings_filepath is None:

                # Create a CarlaSettings object. This object is a wrapper around
                # the CarlaSettings.ini file. Here we set the configuration we
                # want for the new episode.
                settings = CarlaSettings()
                settings.set(
                    SynchronousMode=True,
                    SendNonPlayerAgentsInfo=True,
                    NumberOfVehicles=0,
                    NumberOfPedestrians=40,
                    WeatherId=random.choice([1]),
                    QualityLevel=args.quality_level)
                settings.randomize_seeds()

                # Now we want to add a couple of cameras to the player vehicle.
                # We will collect the images produced by these cameras every
                # frame.

                # The default camera captures RGB images of the scene.
                camera0 = Camera('CameraRGB')
                # Set image resolution in pixels.
                camera0.set_image_size(800, 600)
                # Set its position relative to the car in meters.
                camera0.set_position(0.30, 0, 1.30)
                settings.add_sensor(camera0)

                # Let's add another camera producing ground-truth depth.
                camera1 = Camera('CameraDepth', PostProcessing='Depth')
                camera1.set_image_size(200, 150)
                camera1.set_position(2.30, 0, 1.30)
                settings.add_sensor(camera1)

            else:

                # Alternatively, we can load these settings from a file.
                with open(args.settings_filepath, 'r') as fp:
                    settings = fp.read()

            # Now we load these settings into the server. The server replies
            # with a scene description containing the available start spots for
            # the player. Here we can provide a CarlaSettings object or a
            # CarlaSettings.ini file as string.
            scene = client.load_settings(settings)

            # Choose one player start at random.
            number_of_player_starts = len(scene.player_start_spots)
            player_start = random.randint(0, max(0, number_of_player_starts - 1))

            # Notify the server that we want to start the episode at the
            # player_start index. This function blocks until the server is ready
            # to start the episode.
            print('Starting new episode...')
            client.start_episode(player_start)

            # Iterate every frame in the episode.
            for frame in range(0, frames_per_episode):

                # Read the data produced by the server this frame.
                measurements, sensor_data = client.read_data()

                # Print some of the measurements.
                print_measurements(measurements)

                # Get current location
                location = np.array([
                    measurements.player_measurements.transform.location.x,
                    measurements.player_measurements.transform.location.y,
                ])

                # Get closest point's distance
                dists = norm(pts_2D - location, axis=1)
                which_closest = np.argmin(dists)
                closest, next = pts_2D[which_closest], pts_2D[which_closest+1]
                road_direction = next - closest
                perpendicular = np.array([
                    -road_direction[1],
                    road_direction[0],
                ])
                steer_direction = (location-closest).dot(perpendicular)
                prev_prev_prop = prev_prop
                prev_prop = curr_prop
                curr_prop = np.sign(steer_direction) * dists[which_closest]

                if any(pd.isnull([prev_prev_prop, prev_prop, curr_prop])):
                    deriv = 0
                else:
                    deriv = 0.5 * (curr_prop - prev_prev_prop)
                    deriv_list.append(deriv)
                    if len(deriv_list) > deriv_len:
                        deriv_list = deriv_list[-deriv_len:]
                    deriv = np.mean(deriv_list)

                prev_prev_speed = prev_speed
                prev_speed = curr_speed
                curr_speed = measurements.player_measurements.forward_speed * 3.6
                # TODO: find a better way of keeping the speed constant
                throttle = np.clip(
                    throttle - 0.1 * (curr_speed-target_speed),
                    0.25,
                    1.0
                )

                steer = -kp * curr_prop - kd * deriv + np.random.uniform(-0.05, 0.05)

                print(
                    ' steer_direction = {:.2f}'
                    ' prop = {:.2f}'
                    ' deriv = {:.5f}'
                    ' throttle = {:.2f}'
                    ' curr_speed = {:.2f}'
                    ' steer = {:.2f}'
                    .format(steer_direction, curr_prop, deriv, throttle, curr_speed, steer)
                )

                client.send_control(
                    steer=steer,
                    throttle=throttle,
                    brake=0.0,
                    hand_brake=False,
                    reverse=False)

                depth_array = np.log(sensor_data['CameraDepth'].data).astype('float16')
                storage[..., frame] = depth_array
                stream.write(
                    '{},{},{},{}\n'
                    .format(frame, steer, throttle, curr_speed)
                )

            np.save('depth_data{}.npy'.format(episode), storage)
            stream.close()


def print_measurements(measurements):
    number_of_agents = len(measurements.non_player_agents)
    player_measurements = measurements.player_measurements
    message = 'Vehicle at ({pos_x:.1f}, {pos_y:.1f}), '
    message += '{speed:.0f} km/h, '
    message = message.format(
        pos_x=player_measurements.transform.location.x,
        pos_y=player_measurements.transform.location.y,
        speed=player_measurements.forward_speed * 3.6, # m/s -> km/h
    )
    print_over_same_line(message)


def main():
    argparser = argparse.ArgumentParser(description=__doc__)
    argparser.add_argument(
        '-v', '--verbose',
        action='store_true',
        dest='debug',
        help='print debug information')
    argparser.add_argument(
        '--host',
        metavar='H',
        default='localhost',
        help='IP of the host server (default: localhost)')
    argparser.add_argument(
        '-p', '--port',
        metavar='P',
        default=2000,
        type=int,
        help='TCP port to listen to (default: 2000)')
    argparser.add_argument(
        '-q', '--quality-level',
        choices=['Low', 'Epic'],
        type=lambda s: s.title(),
        default='Epic',
        help='graphics quality level, a lower level makes the simulation run considerably faster.')
    argparser.add_argument(
        '-c', '--carla-settings',
        metavar='PATH',
        dest='settings_filepath',
        default=None,
        help='Path to a "CarlaSettings.ini" file')

    argparser.add_argument(
        '-kp', '--k-prop',
        default=0.0,
        type=float,
        dest='kp',
        help='PID`s controller "proportion" coefficient')
    argparser.add_argument(
        '-kd', '--k-deriv',
        default=0.0,
        type=float,
        dest='kd',
        help='PID`s controller "derivative" coefficient')
    argparser.add_argument(
        '-s', '--speed',
        default=30,
        type=float,
        dest='target_speed',
        help='Target speed')

    args = argparser.parse_args()

    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(format='%(levelname)s: %(message)s', level=log_level)

    logging.info('listening to server %s:%s', args.host, args.port)

    args.out_filename_format = '_out/episode_{:0>4d}/{:s}/{:0>6d}'

    while True:
        try:

            run_carla_client(args)

            print('Done.')
            return

        except TCPConnectionError as error:
            logging.error(error)
            time.sleep(1)


if __name__ == '__main__':

    try:
        main()
    except KeyboardInterrupt:
        print('\nCancelled by user. Bye!')