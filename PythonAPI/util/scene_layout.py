#!/usr/bin/env python

# Copyright (c) 2019 Computer Vision Center (CVC) at the Universitat Autonoma de
# Barcelona (UAB).
#
# This work is licensed under the terms of the MIT license.
# For a copy, see <https://opensource.org/licenses/MIT>.
# Provides map data for users.

# ==============================================================================
# -- imports -------------------------------------------------------------------
# ==============================================================================

import glob
import os
import sys

try:
    sys.path.append(glob.glob('../**/carla-*%d.%d-%s.egg' % (
        sys.version_info.major,
        sys.version_info.minor,
        'win-amd64' if os.name == 'nt' else 'linux-x86_64'))[0])
except IndexError:
    pass


import carla
from carla import TrafficLightState as tls

import argparse
import logging
import datetime
import weakref
import math
import random
import time


def get_scene_layout(world, carla_map):

    """
    Function to extract the full scene layout to be used as a full scene description to be
    given to the user
    :param world: the world object from CARLA
    :return: a dictionary describing the scene.
    """
    def _lateral_shift(transform, shift):
        transform.rotation.yaw += 90
        return transform.location + shift * transform.get_forward_vector()

    topology = [x[0] for x in carla_map.get_topology()]
    topology = sorted(topology, key=lambda w: w.transform.location.z)
    
    # A road contains a list of lanes, a each lane contains a list of waypoints
    map_dict = dict()
    precision = 0.05
    for waypoint in topology:
        waypoints = [waypoint]
        nxt = waypoint.next(precision)[0]
        while nxt.road_id == waypoint.road_id:
            waypoints.append(nxt)
            nxt = nxt.next(precision)[0]

        left_marking = [_lateral_shift(w.transform, -w.lane_width * 0.5) for w in waypoints]
        right_marking = [_lateral_shift(w.transform, w.lane_width * 0.5) for w in waypoints]

        lane = {
            "waypoints": waypoints,
            "left_marking": left_marking,
            "right_marking": right_marking
        }

        if map_dict.get(waypoint.road_id) is None:
            map_dict[waypoint.road_id] = {}
        map_dict[waypoint.road_id][waypoint.lane_id] = lane


    # Generate waypoints graph
    waypoints_graph = dict()
    for road_key in map_dict:
        for lane_key in map_dict[road_key]:
            # List of waypoints
            lane = map_dict[road_key][lane_key]
            
            for i in range(0, len(lane["waypoints"])):
                next_ids = [w.id for w in lane["waypoints"][i+1:len(lane["waypoints"])]]
                
                # Get left and right lane keys
                left_lane_key = lane_key - 1 if lane_key - 1 != 0 else lane_key - 2
                right_lane_key = lane_key + 1 if lane_key + 1 != 0 else lane_key + 2
                
                # Get left and right waypoint ids only if they are valid
                left_lane_waypoint_id = -1
                if left_lane_key in map_dict[road_key]:
                    left_lane_waypoint_id = map_dict[road_key][left_lane_key]["waypoints"][i].id
                
                right_lane_waypoint_id = -1
                if right_lane_key in map_dict[road_key]:
                    right_lane_waypoint_id = map_dict[road_key][right_lane_key]["waypoints"][i].id

                # Get left and right margins (aka markings)
                lm = carla_map.transform_to_geolocation(lane["left_marking"][i])
                rm = carla_map.transform_to_geolocation(lane["right_marking"][i])

                # Waypoint Position
                wl = carla_map.transform_to_geolocation(lane["waypoints"][i].transform.location)

                # Waypoint Orientation
                wo = lane["waypoints"][i].transform.rotation
                
                # Waypoint dict
                waypoint_dict = {
                    "road_id" : road_key,
                    "lane_id" : lane_key,
                    "position": [wl.latitude, wl.longitude, wl.altitude],
                    "orientation": [wo.roll, wo.pitch, wo.yaw],
                    "left_margin_position": [lm.latitude, lm.longitude, lm.altitude],
                    "right_margin_position": [rm.latitude,rm.longitude,rm.altitude],
                    "next_waypoints_ids": next_ids,
                    "left_lane_waypoint_id": left_lane_waypoint_id,
                    "right_lane_waypoint_id": right_lane_waypoint_id
                }
                waypoints_graph[map_dict[road_key][lane_key]["waypoints"][i].id] = waypoint_dict
    
    return waypoints_graph

def get_dynamic_objects(carla_world, carla_map):
    # Private helper functions
    def _get_bounding_box(actor):
        bb = actor.bounding_box.extent
        corners = [
            carla.Location(x=-bb.x, y=-bb.y),
            carla.Location(x=bb.x, y=-bb.y),
            carla.Location(x=bb.x, y=bb.y),
            carla.Location(x=-bb.x, y=bb.y)]
        t = actor.get_transform()
        t.transform(corners)
        corners = [carla_map.transform_to_geolocation(p) for p in corners]
        return corners

    def _get_trigger_volume(actor):
        bb = actor.trigger_volume.extent
        corners = [carla.Location(x=-bb.x, y=-bb.y),
                  carla.Location(x=bb.x, y=-bb.y),
                  carla.Location(x=bb.x, y=bb.y),
                  carla.Location(x=-bb.x, y=bb.y),
                  carla.Location(x=-bb.x, y=-bb.y)]
        corners = [x + actor.trigger_volume.location for x in corners]
        t = actor.get_transform()
        t.transform(corners)
        corners = [carla_map.transform_to_geolocation(p) for p in corners]
        return corners

    def _split_actors(actors):
        vehicles = []
        traffic_lights = []
        speed_limits = []
        walkers = []
        stops = []
        for actor in actors:
            if 'vehicle' in actor.type_id:
                vehicles.append(actor)
            elif 'traffic_light' in actor.type_id:
                traffic_lights.append(actor)
            elif 'speed_limit' in actor.type_id:
                speed_limits.append(actor)
            elif 'walker' in actor.type_id:
                walkers.append(actor)
            elif 'stop' in actor.type_id:
                stops.append(actor)
        return (vehicles, traffic_lights, speed_limits, walkers, stops)

    # Public functions
    def get_stop_signals(stops):
        stop_signals_dict = dict()
        for stop in stops:
            st_transform = stop.get_transform()
            location_gnss = carla_map.transform_to_geolocation(st_transform.location)
            st_dict = {
                "id": stop.id,
                "position": [location_gnss.latitude, location_gnss.longitude, location_gnss.altitude],
                "trigger_volume": [ [v.longitude,v.latitude,v.altitude] for v in _get_trigger_volume(stop)]
            }
            stop_signals_dict[stop.id] = st_dict
        return stop_signals_dict

    def get_traffic_lights(traffic_lights):
        traffic_lights_dict = dict()
        for traffic_light in traffic_lights:
            tl_transform = traffic_light.get_transform()
            location_gnss = carla_map.transform_to_geolocation(tl_transform.location)
            tl_dict = {
                "id": traffic_light.id,
                "state": int(traffic_light.state),
                "position": [location_gnss.latitude, location_gnss.longitude, location_gnss.altitude],
                "trigger_volume": [ [v.longitude,v.latitude,v.altitude] for v in _get_trigger_volume(traffic_light)]
            }
            traffic_lights_dict[traffic_light.id] = tl_dict
        return traffic_lights_dict
  
    def get_vehicles(vehicles):
        vehicles_dict = dict()
        for vehicle in vehicles:
            v_transform = vehicle.get_transform()
            location_gnss = carla_map.transform_to_geolocation(v_transform.location)
            v_dict = {
                "id": vehicle.id,
                "position": [location_gnss.latitude, location_gnss.longitude, location_gnss.altitude],
                "orientation": [v_transform.rotation.roll, v_transform.rotation.pitch, v_transform.rotation.yaw],
                "bounding_box": [ [v.longitude,v.latitude,v.altitude] for v in _get_bounding_box(vehicle)]
            }
            vehicles_dict[vehicle.id] = v_dict
        return vehicles_dict

    def get_hero_vehicle(hero_vehicle):
        if hero_vehicle is None:
            return hero_vehicle
        
        hero_waypoint = carla_map.get_waypoint(hero_vehicle.get_location())
        hero_transform = hero_vehicle.get_transform()
        location_gnss = carla_map.transform_to_geolocation(hero_transform.location)

        hero_vehicle_dict = {
            "id": hero_vehicle.id,
            "position": [location_gnss.latitude, location_gnss.longitude, location_gnss.altitude],
            "road_id": hero_waypoint.road_id,
            "lane_id": hero_waypoint.lane_id
        }
        return hero_vehicle_dict
    
    def get_walkers(walkers):
        walkers_dict = dict()
        for walker in walkers:
            w_transform = walker.get_transform()
            location_gnss = carla_map.transform_to_geolocation(w_transform.location)
            w_dict = {
                "id": walker.id,
                "position": [location_gnss.latitude, location_gnss.longitude, location_gnss.altitude],
                "orientation": [w_transform.rotation.roll, w_transform.rotation.pitch, w_transform.rotation.yaw],
                "bounding_box": [ [v.longitude,v.latitude,v.altitude] for v in _get_bounding_box(walker)]
            }
            walkers_dict[walker.id] = w_dict
        return walkers_dict

    def get_speed_limits(speed_limits):
        speed_limits_dict = dict()
        for speed_limit in speed_limits:
            sl_transform = speed_limit.get_transform()
            location_gnss = carla_map.transform_to_geolocation(sl_transform.location)
            sl_dict = {
                "id": speed_limit.id,
                "position": [location_gnss.latitude, location_gnss.longitude, location_gnss.altitude],
                "speed": int(speed_limit.type_id.split('.')[2])
            }
            speed_limits_dict[speed_limit.id] = sl_dict
        return speed_limits_dict

    
    actors = carla_world.get_actors()
    vehicles, traffic_lights, speed_limits, walkers, stops = _split_actors(actors)

    hero_vehicles = [vehicle for vehicle in vehicles if 'vehicle' in vehicle.type_id and vehicle.attributes['role_name'] == 'hero']
    hero = None if len(hero_vehicles) == 0 else random.choice(hero_vehicles)
  
    return {
        'vehicles': get_vehicles(vehicles),
        'hero_vehicle': get_hero_vehicle(hero),
        'walkers': get_walkers(walkers),
        'traffic_lights': get_traffic_lights(traffic_lights),
        'stop_signs': get_stop_signals(stops),
        'speed_limits': get_speed_limits(speed_limits)
    }



# ==============================================================================
# -- TESTING Map Generation ---------------------------------------------------------------------
# ==============================================================================


class World(object):
    def __init__(self, host, port, timeout):
        self.world, self.town_map = self._get_data_from_carla(host, port, timeout)
        # print(get_scene_layout(self.world, self.town_map))
    
    def _get_data_from_carla(self, host, port, timeout):
        try:
            self.client = carla.Client(host, port)
            self.client.set_timeout(timeout)

            world = self.client.get_world()
            town_map = world.get_map()
            return (world, town_map)

        except Exception as ex:
            logging.error(ex)

   
        
    def tick(self):
      # print(get_dynamic_objects(self.world, self.town_map))
      pass
# ==============================================================================
# -- main() --------------------------------------------------------------------
# ==============================================================================

def main():
    argparser = argparse.ArgumentParser(
        description='CARLA Map Data Extractor')
    argparser.add_argument(
        '-v', '--verbose',
        action='store_true',
        dest='debug',
        help='print debug information')
    argparser.add_argument(
        '--host',
        metavar='H',
        default='127.0.0.1',
        help='IP of the host server (default: 127.0.0.1)')
    argparser.add_argument(
        '-p', '--port',
        metavar='P',
        default=2000,
        type=int,
        help='TCP port to listen to (default: 2000)')
    argparser.add_argument(
        '--filter',
        metavar='PATTERN',
        default='vehicle.*',
        help='actor filter (default: "vehicle.*")')
    argparser.add_argument(
        '--res',
        metavar='WIDTHxHEIGHT',
        default='1280x720',
        help='window resolution (default: 1280x720)')
    args = argparser.parse_args()
    args.width, args.height = [int(x) for x in args.res.split('x')]

    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(format='%(levelname)s: %(message)s', level=log_level)
    logging.info('listening to server %s:%s', args.host, args.port)

    client = carla.Client(args.host, args.port)
    client.set_timeout(2.0)

    world = World(args.host, args.port, 2.0)

    while True:
        world.tick()

    print(__doc__)
    

if __name__ == '__main__':
    main()
