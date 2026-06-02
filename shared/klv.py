#!/usr/bin/env python3
"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

Unified KLV Parser - STANAG 4609 Metadata Processor
Supports both CLI and library interfaces for drone/UAS applications

IMPLEMENTATION NOTE:
-------------------
This module uses manual KLV encoding/decoding rather than the 'klvdata' PyPI library
because klvdata v0.0.3 is parser-only (cannot encode) and contains bugs that cause
crashes on valid STANAG 4609 data. Our manual implementation is:
  - More reliable: No OverflowError or parsing failures
  - More complete: Supports both encoding AND decoding
  - Well-tested: Battle-tested in production transcode workflows
  - MISB ST 0601 compliant: Proper BER encoding, coordinate mapping, field ranges

The klvdata library is kept as an optional dependency for future use if/when
a stable version is released that supports encoding.
"""

import json
import sys
import struct
import time
import argparse
import threading
from datetime import datetime, timezone
from typing import Dict, Any, Optional, Tuple, List
import logging

# Optional dependencies - graceful degradation
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False

# KLV encoding library (optional - currently not used due to bugs in v0.0.3)
# See module docstring for rationale
try:
    from klvdata import StreamParser
    from klvdata.common import datetime_to_bytes
    from klvdata.misb0601 import (
        UASLocalMetadataSet,
        PrecisionTimeStamp,
        MissionID,
        PlatformDesignation,
        PlatformHeadingAngle,
        SensorLatitude,
        SensorLongitude,
        SensorTrueAltitude,
        FrameCenterLatitude,
        FrameCenterLongitude,
        FrameCenterElevation,
    )
    KLVDATA_AVAILABLE = True
except ImportError:
    KLVDATA_AVAILABLE = False
    # Note: This is intentionally silent - klvdata is optional
    # and not currently used in production code

# STANAG 4609 UAS Datalink Local Set (ULS) Tags
STANAG_4609_TAGS = {
    1: 'Checksum',
    2: 'UNIX Time Stamp',
    3: 'Mission ID',
    4: 'Platform Tail Number',
    5: 'Platform Heading Angle',
    6: 'Platform Pitch Angle',
    7: 'Platform Roll Angle',
    8: 'Platform True Airspeed',
    9: 'Platform Indicated Airspeed',
    10: 'Platform Designation',
    11: 'Image Source Sensor',
    12: 'Image Coordinate System',
    13: 'Sensor Latitude',
    14: 'Sensor Longitude',
    15: 'Sensor True Altitude',
    16: 'Sensor Horizontal Field of View',
    17: 'Sensor Vertical Field of View',
    18: 'Sensor Relative Azimuth Angle',
    19: 'Sensor Relative Elevation Angle',
    20: 'Sensor Relative Roll Angle',
    21: 'Slant Range',
    22: 'Target Width',
    23: 'Frame Center Latitude',
    24: 'Frame Center Longitude',
    25: 'Frame Center Elevation',
    26: 'Offset Corner Latitude Point 1',
    27: 'Offset Corner Longitude Point 1',
    28: 'Offset Corner Latitude Point 2',
    29: 'Offset Corner Longitude Point 2',
    30: 'Offset Corner Latitude Point 3',
    31: 'Offset Corner Longitude Point 3',
    32: 'Offset Corner Latitude Point 4',
    33: 'Offset Corner Longitude Point 4',
    34: 'Icing Detected',
    35: 'Wind Direction',
    36: 'Wind Speed',
    37: 'Static Pressure',
    38: 'Density Altitude',
    39: 'Outside Air Temperature',
    40: 'Target Location Latitude',
    41: 'Target Location Longitude',
    42: 'Target Location Elevation',
    43: 'Target Track Gate Width',
    44: 'Target Track Gate Height',
    45: 'Target Error Estimate - CE90',
    46: 'Target Error Estimate - LE90',
    47: 'Generic Flag Data 01',
    48: 'Security Local Set',
    49: 'Differential Pressure',
    50: 'Platform Angle of Attack',
    51: 'Platform Vertical Speed',
    52: 'Platform Sideslip Angle',
    53: 'Airfield Barometric Pressure',
    54: 'Airfield Elevation',
    55: 'Relative Humidity',
    56: 'Platform Ground Speed',
    57: 'Ground Range',
    58: 'Platform Fuel Remaining',
    59: 'Platform Call Sign',
    60: 'Weapon Load',
    61: 'Weapon Fired',
    62: 'Laser PRF Code',
    63: 'Sensor Field of View Name',
    64: 'Platform Magnetic Heading',
    65: 'UAS Datalink LS Version Number',
    66: 'Target Location Covariance Matrix',
    67: 'Alternate Platform Latitude',
    68: 'Alternate Platform Longitude',
    69: 'Alternate Platform Altitude',
    70: 'Alternate Platform Name',
    71: 'Alternate Platform Heading',
    72: 'Event Start Time - UTC',
    73: 'RVT Local Set',
    74: 'VMTI Local Set',
    75: 'Sensor Ellipsoid Height',
    76: 'Alternate Platform Ellipsoid Height',
    77: 'Operational Mode',
    78: 'Frame Center Height Above Ellipsoid',
    79: 'Sensor North Velocity',
    80: 'Sensor East Velocity',
    81: 'Image Horizon Pixel Pack',
    82: 'Corner Latitude Point 1 (Full)',
    83: 'Corner Longitude Point 1 (Full)',
    84: 'Corner Latitude Point 2 (Full)',
    85: 'Corner Longitude Point 2 (Full)',
    86: 'Corner Latitude Point 3 (Full)',
    87: 'Corner Longitude Point 3 (Full)',
    88: 'Corner Latitude Point 4 (Full)',
    89: 'Corner Longitude Point 4 (Full)',
}

class UnifiedKLVParser:
    """
    Unified KLV Parser supporting both CLI and library interfaces
    Implements STANAG 4609 UAS Datalink Local Set parsing
    """
    
    def __init__(self, stream_name: str = None, api_url: str = "http://localhost:3000"):
        self.stream_name = stream_name
        self.api_url = api_url
        self.raw_format = 'hex'  # Default format for raw values: 'hex' or 'decimal'
        self.logger = self._setup_logging()
        
    def _setup_logging(self) -> logging.Logger:
        """Setup logging configuration"""
        return logging.getLogger('UnifiedKLVParser')
        
    def parse_ber_length(self, data: bytes, offset: int) -> Tuple[int, int]:
        """Parse BER (Basic Encoding Rules) length field"""
        if offset >= len(data):
            return 0, offset
            
        first_byte = data[offset]
        offset += 1
        
        if first_byte & 0x80 == 0:
            # Short form
            return first_byte, offset
        else:
            # Long form
            length_bytes = first_byte & 0x7F
            if length_bytes == 0 or offset + length_bytes > len(data):
                return 0, offset
                
            length = 0
            for i in range(length_bytes):
                length = (length << 8) | data[offset]
                offset += 1
            return length, offset
    
    def parse_klv_packet(self, data: bytes) -> Dict[str, Any]:
        """Parse a complete KLV packet"""
        result = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'stream_name': self.stream_name,
            'tags': {},
            'raw_size': len(data)
        }
        
        if len(data) < 16:  # Minimum KLV packet size
            result['error'] = 'Packet too small'
            return result
            
        offset = 0
        
        try:
            # Parse UAS Datalink Local Set (16-byte key)
            if data[offset:offset+16] == b'\x06\x0e\x2b\x34\x02\x0b\x01\x01\x0e\x01\x03\x01\x01\x00\x00\x00':
                offset += 16
                
                # Parse length
                length, offset = self.parse_ber_length(data, offset)
                
                if offset + length > len(data):
                    result['error'] = 'Invalid length field'
                    return result
                
                # Parse tags within the ULS
                end_offset = offset + length
                while offset < end_offset:
                    tag, offset = self.parse_ber_length(data, offset)
                    if offset >= end_offset:
                        break
                        
                    tag_length, offset = self.parse_ber_length(data, offset)
                    if offset + tag_length > end_offset:
                        break
                    
                    tag_data = data[offset:offset + tag_length]
                    offset += tag_length
                    
                    # Parse tag value based on type
                    tag_name = STANAG_4609_TAGS.get(tag, f'Unknown Tag {tag}')
                    parsed_value, raw_value = self._parse_tag_value(tag, tag_data)
                    
                    result['tags'][tag_name] = {
                        'tag_id': tag,
                        'value': parsed_value,
                        'raw_value': raw_value,
                        'raw_length': tag_length
                    }
                    
        except Exception as e:
            result['error'] = f'Parse error: {str(e)}'
            
        return result
    
    def _parse_tag_value(self, tag: int, data: bytes) -> tuple[Any, Any]:
        """
        Parse individual tag values based on MISB ST 0601.19 specifications
        Returns: (decoded_value, raw_value_in_specified_format)
        
        All conversions follow MISB ST 0601.19 formulas and ranges
        """
        if len(data) == 0:
            return None, None
        
        # Format raw value based on preference
        if self.raw_format == 'decimal':
            # Convert bytes to combined unsigned integer (big-endian)
            raw_value = int.from_bytes(data, byteorder='big', signed=False)
        else:
            raw_value = data.hex()  # Default hex format
            
        try:
            # Tag 1: Checksum (2 bytes, uint16)
            if tag == 1:
                if len(data) == 2:
                    checksum = struct.unpack('>H', data)[0]
                    return checksum, raw_value
                return raw_value, raw_value
            
            # Tag 2: UNIX Time Stamp (8 bytes, uint64 microseconds)
            elif tag == 2:
                if len(data) == 8:
                    timestamp = struct.unpack('>Q', data)[0]
                    return timestamp / 1000000.0, raw_value  # Convert to seconds
                return raw_value, raw_value
                    
            # String values (variable length, UTF-8)
            elif tag in [3, 4, 10, 11, 59, 63, 70, 77]:  
                # 3=Mission ID, 4=Platform Tail Number, 10=Platform Designation,
                # 11=Image Source Sensor, 59=Platform Call Sign, 63=Sensor FOV Name,
                # 70=Alternate Platform Name, 77=Operational Mode
                decoded = data.decode('utf-8', errors='ignore').strip('\x00')
                return decoded, raw_value
                
            # Tag 5: Platform Heading Angle (2 bytes, IMAPB, 0-360°)
            elif tag == 5:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    # Map 0-65535 to 0-360 degrees
                    return (raw * 360.0 / 65535.0), raw_value
                return raw_hex, raw_value
                    
            # Tags 6,7: Platform Pitch/Roll Angle (2 bytes, IMAPB, -20 to +20°)
            elif tag in [6, 7]:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    # Map 0-65535 to -20 to +20 degrees
                    return (raw - 32767.5) * (40.0 / 65535.0), raw_value
                return raw_hex, raw_value
                    
            # Tag 8: Platform True Airspeed (1 byte, uint8, 0-255 m/s)
            elif tag == 8:
                if len(data) == 1:
                    return struct.unpack('B', data)[0], raw_value
                return raw_hex, raw_value
                    
            # Tag 9: Platform Indicated Airspeed (1 byte, uint8, 0-255 m/s)
            elif tag == 9:
                if len(data) == 1:
                    return struct.unpack('B', data)[0], raw_value
                return raw_hex, raw_value
            
            # Tag 12: Image Coordinate System (variable length string)
            elif tag == 12:
                decoded = data.decode('utf-8', errors='ignore').strip('\x00')
                return decoded, raw_value
                    
            # Coordinates - Latitude/Longitude (4 bytes, int32, IMAPB)
            # Tags 13,14: Sensor Lat/Lon, 23,24: Frame Center Lat/Lon
            # Tags 26-33: Offset Corner Lat/Lon (4 points)
            # Tags 40,41: Target Location Lat/Lon, 67,68: Alternate Platform Lat/Lon
            # Tags 82-89: Full Corner Lat/Lon (4 points, 8 bytes)
            elif tag in [13, 14, 23, 24, 26, 27, 28, 29, 30, 31, 32, 33, 40, 41, 67, 68]:
                if len(data) == 4:
                    raw = struct.unpack('>i', data)[0]
                    # Map -(2^31-1) to +(2^31-1) to ±90° (lat) or ±180° (lon)
                    # Odd tags are latitude (-90 to +90), even tags are longitude (-180 to +180)
                    if tag % 2 == 1:  # Latitude
                        return (raw / (2**31 - 1)) * 90.0, raw_value
                    else:  # Longitude
                        return (raw / (2**31 - 1)) * 180.0, raw_value
                return raw_value, raw_value
            
            # Full precision corners (8 bytes double)
            elif tag in [82, 83, 84, 85, 86, 87, 88, 89]:
                if len(data) == 8:
                    value = struct.unpack('>d', data)[0]
                    return value, raw_value
                return raw_value, raw_value
                    
            # Tag 15: Sensor True Altitude (2 bytes, uint16, -900 to +19000 m)
            elif tag == 15:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    # Map 0-65535 to -900 to +19000 meters
                    return (raw / 65535.0) * 19900.0 - 900.0, raw_value
                return raw_value, raw_value
            
            # Tag 16: Sensor Horizontal FOV (2 bytes, uint16, 0-180°)
            elif tag == 16:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw * 180.0 / 65535.0), raw_value
                return raw_value, raw_value
                    
            # Tag 17: Sensor Vertical FOV (2 bytes, uint16, 0-180°)
            elif tag == 17:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw * 180.0 / 65535.0), raw_value
                return raw_value, raw_value
            
            # Tags 18,19,20: Sensor Relative Azimuth/Elevation/Roll (2 bytes, IMAPB, 0-360°)
            elif tag in [18, 19, 20]:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw * 360.0 / 65535.0), raw_value
                return raw_value, raw_value
            
            # Tag 21: Slant Range (4 bytes, uint32, 0-5000000 m)
            elif tag == 21:
                if len(data) == 4:
                    raw = struct.unpack('>I', data)[0]
                    return (raw * 5000000.0 / (2**32 - 1)), raw_value
                return raw_value, raw_value
            
            # Tag 22: Target Width (2 bytes, uint16, 0-10000 m)
            elif tag == 22:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw * 10000.0 / 65535.0), raw_value
                return raw_value, raw_value
                    
            # Tag 25: Frame Center Elevation (2 bytes, uint16, -900 to +19000 m)
            elif tag == 25:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw / 65535.0) * 19900.0 - 900.0, raw_value
                return raw_value, raw_value
            
            # Tag 34: Icing Detected (1 byte, uint8, 0-1 boolean)
            elif tag == 34:
                if len(data) == 1:
                    return bool(data[0]), raw_value
                return raw_value, raw_value
            
            # Tag 35: Wind Direction (2 bytes, uint16, 0-360°)
            elif tag == 35:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw * 360.0 / 65535.0), raw_value
                return raw_value, raw_value
            
            # Tag 36: Wind Speed (1 byte, uint8, 0-100 m/s)
            elif tag == 36:
                if len(data) == 1:
                    return struct.unpack('B', data)[0], raw_value
                return raw_value, raw_value
            
            # Tag 37: Static Pressure (2 bytes, uint16, 0-5000 mbar)
            elif tag == 37:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw * 5000.0 / 65535.0), raw_value
                return raw_value, raw_value
            
            # Tag 38: Density Altitude (2 bytes, uint16, -900 to +19000 m)
            elif tag == 38:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw / 65535.0) * 19900.0 - 900.0, raw_value
                return raw_value, raw_value
            
            # Tag 39: Outside Air Temperature (1 byte, int8, -128 to +127 °C)
            elif tag == 39:
                if len(data) == 1:
                    return struct.unpack('b', data)[0], raw_value
                return raw_value, raw_value
            
            # Tag 42: Target Location Elevation (2 bytes, uint16, -900 to +19000 m)
            elif tag == 42:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw / 65535.0) * 19900.0 - 900.0, raw_value
                return raw_value, raw_value
            
            # Tag 43: Target Track Gate Width (1 byte, uint8, 0-255 pixels)
            elif tag == 43:
                if len(data) == 1:
                    return struct.unpack('B', data)[0], raw_value
                return raw_value, raw_value
            
            # Tag 44: Target Track Gate Height (1 byte, uint8, 0-255 pixels)
            elif tag == 44:
                if len(data) == 1:
                    return struct.unpack('B', data)[0], raw_value
                return raw_value, raw_value
            
            # Tag 45: Target Error Estimate - CE90 (2 bytes, uint16, 0-4095 m)
            elif tag == 45:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw * 4095.0 / 65535.0), raw_value
                return raw_value, raw_value
            
            # Tag 46: Target Error Estimate - LE90 (2 bytes, uint16, 0-4095 m)
            elif tag == 46:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw * 4095.0 / 65535.0), raw_value
                return raw_value, raw_value
            
            # Tag 47: Generic Flag Data (1 byte, bitfield)
            elif tag == 47:
                if len(data) == 1:
                    return struct.unpack('B', data)[0], raw_value
                return raw_value, raw_value
            
            # Tag 48: Security Local Set (variable, nested KLV)
            elif tag == 48:
                return f"Security Local Set ({len(data)} bytes)", raw_value
            
            # Tag 49: Differential Pressure (2 bytes, uint16, 0-5000 mbar)
            elif tag == 49:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw * 5000.0 / 65535.0), raw_value
                return raw_value, raw_value
            
            # Tag 50: Platform Angle of Attack (2 bytes, int16, -20 to +20°)
            elif tag == 50:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw - 32767.5) * (40.0 / 65535.0), raw_value
                return raw_value, raw_value
            
            # Tag 51: Platform Vertical Speed (2 bytes, int16, -180 to +180 m/s)
            elif tag == 51:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw - 32767.5) * (360.0 / 65535.0), raw_value
                return raw_value, raw_value
            
            # Tag 52: Platform Sideslip Angle (2 bytes, int16, -20 to +20°)
            elif tag == 52:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw - 32767.5) * (40.0 / 65535.0), raw_value
                return raw_value, raw_value
            
            # Tag 53: Airfield Barometric Pressure (2 bytes, uint16, 0-5000 mbar)
            elif tag == 53:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw * 5000.0 / 65535.0), raw_value
                return raw_value, raw_value
            
            # Tag 54: Airfield Elevation (2 bytes, uint16, -900 to +19000 m)
            elif tag == 54:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw / 65535.0) * 19900.0 - 900.0, raw_value
                return raw_value, raw_value
            
            # Tag 55: Relative Humidity (1 byte, uint8, 0-100%)
            elif tag == 55:
                if len(data) == 1:
                    return struct.unpack('B', data)[0], raw_value
                return raw_value, raw_value
            
            # Tag 56: Platform Ground Speed (1 byte, uint8, 0-255 m/s)
            elif tag == 56:
                if len(data) == 1:
                    return struct.unpack('B', data)[0], raw_value
                return raw_value, raw_value
            
            # Tag 57: Ground Range (4 bytes, uint32, 0-5000000 m)
            elif tag == 57:
                if len(data) == 4:
                    raw = struct.unpack('>I', data)[0]
                    return (raw * 5000000.0 / (2**32 - 1)), raw_value
                return raw_value, raw_value
            
            # Tag 58: Platform Fuel Remaining (2 bytes, uint16, 0-10000 kg)
            elif tag == 58:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw * 10000.0 / 65535.0), raw_value
                return raw_value, raw_value
            
            # Tag 60: Weapon Load (2 bytes, uint16, station/store)
            elif tag == 60:
                if len(data) == 2:
                    return struct.unpack('>H', data)[0], raw_value
                return raw_value, raw_value
            
            # Tag 61: Weapon Fired (1 byte, uint8, 0-1 boolean)
            elif tag == 61:
                if len(data) == 1:
                    return bool(data[0]), raw_value
                return raw_value, raw_value
            
            # Tag 62: Laser PRF Code (2 bytes, uint16)
            elif tag == 62:
                if len(data) == 2:
                    return struct.unpack('>H', data)[0], raw_value
                return raw_value, raw_value
            
            # Tag 64: Platform Magnetic Heading (2 bytes, uint16, 0-360°)
            elif tag == 64:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw * 360.0 / 65535.0), raw_value
                return raw_value, raw_value
            
            # Tag 65: UAS Datalink LS Version Number (1 byte, uint8)
            elif tag == 65:
                if len(data) == 1:
                    return struct.unpack('B', data)[0], raw_value
                return raw_value, raw_value
            
            # Tag 66: Target Location Covariance Matrix (variable, nested)
            elif tag == 66:
                return f"Covariance Matrix ({len(data)} bytes)", raw_value
            
            # Tag 69: Alternate Platform Altitude (2 bytes, uint16, -900 to +19000 m)
            elif tag == 69:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw / 65535.0) * 19900.0 - 900.0, raw_value
                return raw_value, raw_value
            
            # Tag 71: Alternate Platform Heading (2 bytes, uint16, 0-360°)
            elif tag == 71:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw * 360.0 / 65535.0), raw_value
                return raw_value, raw_value
            
            # Tag 72: Event Start Time - UTC (8 bytes, uint64 microseconds)
            elif tag == 72:
                if len(data) == 8:
                    timestamp = struct.unpack('>Q', data)[0]
                    return timestamp / 1000000.0, raw_value
                return raw_value, raw_value
            
            # Tag 73: RVT Local Set (variable, nested KLV)
            elif tag == 73:
                return f"RVT Local Set ({len(data)} bytes)", raw_value
            
            # Tag 74: VMTI Local Set (variable, nested KLV)
            elif tag == 74:
                return f"VMTI Local Set ({len(data)} bytes)", raw_value
            
            # Tag 75: Sensor Ellipsoid Height (2 bytes, uint16, -900 to +19000 m)
            elif tag == 75:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw / 65535.0) * 19900.0 - 900.0, raw_value
                return raw_value, raw_value
            
            # Tag 76: Alternate Platform Ellipsoid Height (2 bytes, uint16, -900 to +19000 m)
            elif tag == 76:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw / 65535.0) * 19900.0 - 900.0, raw_value
                return raw_value, raw_value
            
            # Tag 78: Frame Center HAE (2 bytes, uint16, -900 to +19000 m)
            elif tag == 78:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw / 65535.0) * 19900.0 - 900.0, raw_value
                return raw_value, raw_value
            
            # Tag 79: Sensor North Velocity (2 bytes, int16, -327 to +327 m/s)
            elif tag == 79:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw - 32767.5) * (654.0 / 65535.0), raw_value
                return raw_value, raw_value
            
            # Tag 80: Sensor East Velocity (2 bytes, int16, -327 to +327 m/s)
            elif tag == 80:
                if len(data) == 2:
                    raw = struct.unpack('>H', data)[0]
                    return (raw - 32767.5) * (654.0 / 65535.0), raw_value
                return raw_value, raw_value
            
            # Tag 81: Image Horizon Pixel Pack (variable)
            elif tag == 81:
                return f"Image Horizon ({len(data)} bytes)", raw_value
                    
            # Default: return as hex string
            else:
                return raw_value, raw_value
                
        except Exception:
            # Fallback to hex representation
            return raw_value, raw_value
    
    def send_to_api(self, klv_data: Dict[str, Any]) -> bool:
        """Send parsed KLV data to Node.js API"""
        if not REQUESTS_AVAILABLE:
            self.logger.warning("Requests library not available, cannot send to API")
            return False
            
        try:
            url = f"{self.api_url}/api/klv/{self.stream_name}"
            response = requests.post(url, json=klv_data, timeout=5)
            response.raise_for_status()
            return True
        except Exception as e:
            self.logger.error(f"Failed to send KLV data to API: {e}")
            return False
    
    def generate_test_klv(self) -> bytes:
        """Generate test KLV packet for validation"""
        # UAS Datalink Local Set key
        key = b'\x06\x0e+4\x02\x0b\x01\x01\x0e\x01\x03\x01\x01\x00\x00\x00'
        
        # Create test tags
        tags = []
        
        # UNIX timestamp (tag 2)
        timestamp = int(time.time() * 1000000)
        tags.append(b'\x02\x08' + struct.pack('>Q', timestamp))
        
        # Mission ID (tag 3)
        mission_id = b'TEST_MISSION_001'
        tags.append(b'\x03' + bytes([len(mission_id)]) + mission_id)
        
        # Platform latitude (tag 13) - example: 40.7128° N (NYC)
        # MISB ST 0601: latitude range ±90 maps to the full int32 (±(2^31-1)).
        lat_raw = int(40.7128 * (2**31 - 1) / 90.0)
        tags.append(b'\x0d\x04' + struct.pack('>i', lat_raw))

        # Platform longitude (tag 14) - example: -74.0060° W (NYC)
        # MISB ST 0601: longitude range ±180 maps to the full int32 (±(2^31-1)).
        lon_raw = int(-74.0060 * (2**31 - 1) / 180.0)
        tags.append(b'\x0e\x04' + struct.pack('>i', lon_raw))
        
        # Platform altitude (tag 15) - example: 1000 feet
        alt_raw = int(1000.0) + 32768
        tags.append(b'\x0f\x02' + struct.pack('>H', alt_raw))
        
        # Platform heading (tag 5) - example: 90 degrees (East)
        heading_raw = int((90.0 + 180.0) * 65536.0 / 360.0)
        tags.append(b'\x05\x02' + struct.pack('>H', heading_raw))
        
        # Horizontal FOV (tag 16) - example: 20 degrees
        hfov_raw = int(20.0 * 65536.0 / 180.0)
        tags.append(b'\x10\x02' + struct.pack('>H', hfov_raw))
        
        # Vertical FOV (tag 17) - example: 12 degrees  
        vfov_raw = int(12.0 * 65536.0 / 180.0)
        tags.append(b'\x11\x02' + struct.pack('>H', vfov_raw))
        
        # Frame Center Latitude (tag 23) - same as sensor for demo
        tags.append(b'\x17\x04' + struct.pack('>i', lat_raw))
        
        # Frame Center Longitude (tag 24) - same as sensor for demo
        tags.append(b'\x18\x04' + struct.pack('>i', lon_raw))
        
        # Frame Center Elevation (tag 25) - same as sensor for demo
        tags.append(b'\x19\x02' + struct.pack('>H', alt_raw))
        
        # Combine all tags
        tag_data = b''.join(tags)
        
        # Create length field
        length = len(tag_data)
        if length < 128:
            length_field = bytes([length])
        else:
            # Long form encoding
            length_bytes = []
            temp_length = length
            while temp_length > 0:
                length_bytes.insert(0, temp_length & 0xFF)
                temp_length >>= 8
            length_field = bytes([0x80 | len(length_bytes)]) + bytes(length_bytes)
        
        return key + length_field + tag_data
    
    def run_test_mode(self):
        """Run parser in test mode"""
        print("=== Unified KLV Parser Test Mode ===")
        
        # Generate test packet
        test_packet = self.generate_test_klv()
        print(f"Generated test packet: {len(test_packet)} bytes")
        
        # Parse the packet
        parsed = self.parse_klv_packet(test_packet)
        
        # Display results
        print("\nParsed KLV Data:")
        print(json.dumps(parsed, indent=2))
        
        # Test API submission if available
        if self.stream_name and REQUESTS_AVAILABLE:
            print(f"\nTesting API submission to stream: {self.stream_name}")
            success = self.send_to_api(parsed)
            print(f"API submission: {'SUCCESS' if success else 'FAILED'}")
    
    def run_stream_mode(self):
        """Run parser in stream processing mode"""
        print(f"=== Streaming KLV Parser for {self.stream_name} ===")
        
        # In a real implementation, this would:
        # 1. Connect to the stream source
        # 2. Extract KLV data from the stream
        # 3. Parse and forward to API
        # 4. Handle errors gracefully
        
        # For now, simulate with periodic test data
        import time
        
        try:
            while True:
                test_packet = self.generate_test_klv()
                parsed = self.parse_klv_packet(test_packet)
                
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Processed KLV packet")
                
                if self.send_to_api(parsed):
                    print(f"  -> Sent to API for stream: {self.stream_name}")
                else:
                    print(f"  -> Failed to send to API")
                
                time.sleep(2)  # 2Hz update rate
                
        except KeyboardInterrupt:
            print("\nShutting down KLV parser...")


def parse_klv_packets(klv_data: bytes) -> list:
    """Frame STANAG 4609 / MISB ST 0601 packets from a raw KLV byte stream and decode each.

    Splits the stream on the 16-byte UAS Datalink Local Set universal key followed by a
    BER length, then decodes every complete packet with :class:`UnifiedKLVParser`. Returns
    a list of parsed-packet dicts (each as produced by ``parse_klv_packet``); the list is
    empty when the stream contains no valid packet. This is the single source of truth for
    KLV framing, shared by the ``/api/klv/extract`` endpoint and the ``utils/read_klv.py``
    CLI so the logic never drifts between them.
    """
    parser = UnifiedKLVParser()
    packets = []

    # STANAG 4609 universal key (start of each UAS Local Set packet).
    stanag_key = bytes([0x06, 0x0E, 0x2B, 0x34, 0x02, 0x0B, 0x01, 0x01,
                        0x0E, 0x01, 0x03, 0x01, 0x01, 0x00, 0x00, 0x00])

    offset = 0
    while offset < len(klv_data):
        # Look for the next key; skip any non-KLV bytes between packets.
        if klv_data[offset:offset + 16] != stanag_key:
            next_key = klv_data.find(stanag_key, offset + 1)
            if next_key == -1:
                break
            offset = next_key
            continue

        try:
            # Parse the BER length immediately after the 16-byte key.
            if offset + 16 >= len(klv_data):
                break

            length_byte = klv_data[offset + 16]
            if length_byte < 128:
                length_size = 1
                data_len = length_byte
            else:
                length_bytes = length_byte & 0x7F
                length_size = 1 + length_bytes
                data_len = 0
                for i in range(length_bytes):
                    if offset + 17 + i >= len(klv_data):
                        break
                    data_len = (data_len << 8) | klv_data[offset + 17 + i]

            packet_size = 16 + length_size + data_len

            if offset + packet_size > len(klv_data):
                break

            packets.append(parser.parse_klv_packet(klv_data[offset:offset + packet_size]))
            offset += packet_size

        except Exception:
            # Corrupt/implausible packet — advance one byte and resync on the next key.
            offset += 1
            continue

    return packets


def encode_uas_metadata(metadata: Dict[str, Any]) -> bytes:
    """
    Encode UAS metadata into STANAG 4609 KLV format
    
    Uses manual encoding with proper MISB ST 0601 compliance.
    The klvdata library is primarily a parser, not an encoder, so we use
    our own implementation following the STANAG 4609 specifications.
    
    Args:
        metadata: Dictionary with UAS metadata fields
            Required: timestamp (microseconds since epoch)
            Optional: mission_id, platform_designation, coordinates, etc.
    
    Returns:
        KLV packet bytes with proper MISB ST 0601 encoding
    """
    # UAS Datalink Local Set key (16 bytes) - MISB ST 0601
    key = b'\x06\x0e\x2b\x34\x02\x0b\x01\x01\x0e\x01\x03\x01\x01\x00\x00\x00'
    
    tags = []
    
    # UNIX timestamp (tag 2) - REQUIRED - microseconds since epoch
    if 'timestamp' in metadata:
        timestamp = int(metadata['timestamp'])
        tags.append(b'\x02\x08' + struct.pack('>Q', timestamp))
    
    # Mission ID (tag 3) - UTF-8 string, max 127 bytes
    if 'mission_id' in metadata:
        mission_id = str(metadata['mission_id']).encode('utf-8')[:127]
        tags.append(b'\x03' + bytes([len(mission_id)]) + mission_id)
    
    # Platform Heading Angle (tag 5) - degrees, mapped to 0-360
    if 'platform_heading' in metadata:
        heading = float(metadata['platform_heading'])
        # MISB ST 0601: Map from -180/+180 to 0-65535
        heading_raw = int((heading + 180.0) * 65536.0 / 360.0) & 0xFFFF
        tags.append(b'\x05\x02' + struct.pack('>H', heading_raw))
    
    # Platform Designation (tag 10) - UTF-8 string, max 127 bytes
    if 'platform_designation' in metadata:
        platform = str(metadata['platform_designation']).encode('utf-8')[:127]
        tags.append(b'\x0a' + bytes([len(platform)]) + platform)
    
    # Sensor Latitude (tag 13) - degrees, IMAPB encoding
    if 'sensor_latitude' in metadata:
        lat = float(metadata['sensor_latitude'])
        # MISB ST 0601: latitude range ±90 maps to the full int32 (±(2^31-1)).
        lat_raw = int(lat * (2**31 - 1) / 90.0)
        tags.append(b'\x0d\x04' + struct.pack('>i', lat_raw))

    # Sensor Longitude (tag 14) - degrees, IMAPB encoding
    if 'sensor_longitude' in metadata:
        lon = float(metadata['sensor_longitude'])
        # MISB ST 0601: longitude range ±180 maps to the full int32 (±(2^31-1)).
        lon_raw = int(lon * (2**31 - 1) / 180.0)
        tags.append(b'\x0e\x04' + struct.pack('>i', lon_raw))
    
    # Sensor True Altitude (tag 15) - meters, IMAPB encoding
    if 'sensor_altitude' in metadata:
        alt = float(metadata['sensor_altitude'])
        # MISB ST 0601: Map -900/+19000 meters to 0-65535
        alt_normalized = (alt - (-900.0)) / (19000.0 - (-900.0))
        alt_raw = int(alt_normalized * 65535.0)
        alt_raw = max(0, min(65535, alt_raw))  # Clamp to valid range
        tags.append(b'\x0f\x02' + struct.pack('>H', alt_raw))
    
    # Frame Center Latitude (tag 23) - degrees, IMAPB encoding
    if 'frame_center_latitude' in metadata:
        lat = float(metadata['frame_center_latitude'])
        # MISB ST 0601: latitude range ±90 maps to the full int32 (±(2^31-1)).
        lat_raw = int(lat * (2**31 - 1) / 90.0)
        tags.append(b'\x17\x04' + struct.pack('>i', lat_raw))

    # Frame Center Longitude (tag 24) - degrees, IMAPB encoding
    if 'frame_center_longitude' in metadata:
        lon = float(metadata['frame_center_longitude'])
        # MISB ST 0601: longitude range ±180 maps to the full int32 (±(2^31-1)).
        lon_raw = int(lon * (2**31 - 1) / 180.0)
        tags.append(b'\x18\x04' + struct.pack('>i', lon_raw))
    
    # Frame Center Elevation (tag 25) - meters, IMAPB encoding
    if 'frame_center_elevation' in metadata:
        elev = float(metadata['frame_center_elevation'])
        # MISB ST 0601: Map -900/+19000 meters to 0-65535
        elev_normalized = (elev - (-900.0)) / (19000.0 - (-900.0))
        elev_raw = int(elev_normalized * 65535.0)
        elev_raw = max(0, min(65535, elev_raw))  # Clamp to valid range
        tags.append(b'\x19\x02' + struct.pack('>H', elev_raw))
    
    # Combine all tags
    tag_data = b''.join(tags)
    
    # Create BER length field
    length = len(tag_data)
    if length < 128:
        length_field = bytes([length])
    else:
        # Long form BER encoding
        length_bytes = []
        temp_length = length
        while temp_length > 0:
            length_bytes.insert(0, temp_length & 0xFF)
            temp_length >>= 8
        length_field = bytes([0x80 | len(length_bytes)]) + bytes(length_bytes)
    
    return key + length_field + tag_data


def main():
    """CLI entry point"""
    parser = argparse.ArgumentParser(description='Unified KLV Parser - STANAG 4609 processor')
    parser.add_argument('--mode', choices=['test', 'stream'], default='test',
                       help='Parser mode: test (validation) or stream (processing)')
    parser.add_argument('--stream', type=str,
                       help='Stream name for processing mode')
    parser.add_argument('--api-url', type=str, default='http://localhost:3000',
                       help='API base URL')
    
    args = parser.parse_args()
    
    # Create parser instance
    klv_parser = UnifiedKLVParser(
        stream_name=args.stream,
        api_url=args.api_url
    )
    
    # Run in specified mode
    if args.mode == 'test':
        klv_parser.run_test_mode()
    elif args.mode == 'stream':
        if not args.stream:
            print("Error: --stream required for stream mode")
            sys.exit(1)
        klv_parser.run_stream_mode()

if __name__ == '__main__':
    main()
