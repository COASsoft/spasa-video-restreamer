#!/usr/bin/env python3
"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

Read and display KLV metadata from video files or binary KLV files
Decodes STANAG 4609 packets and shows in human-readable format
"""

import sys
import os
import struct
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# Add shared directory to path for KLV import
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))
try:
    from klv import parse_klv_packets
    KLV_AVAILABLE = True
except ImportError:
    KLV_AVAILABLE = False
    print("Error: KLV module not available")
    sys.exit(1)


def is_video_file(file_path):
    """Check if file is a video file"""
    video_extensions = ['.mp4', '.mov', '.ts', '.mkv', '.avi', '.mxf', '.mpeg', '.mpg']
    return Path(file_path).suffix.lower() in video_extensions


def extract_klv_from_video(video_file):
    """Extract KLV data stream from video file to temporary file"""
    print(f"Extracting KLV from video file...")
    
    # Create temporary file for extracted KLV
    temp_klv = tempfile.NamedTemporaryFile(delete=False, suffix='.bin')
    temp_klv.close()
    
    cmd = [
        'ffmpeg',
        '-i', video_file,
        '-map', '0:d',  # Map data stream
        '-c', 'copy',
        '-f', 'data',
        '-y',
        temp_klv.name
    ]
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60
        )
        
        if result.returncode == 0 and os.path.exists(temp_klv.name) and os.path.getsize(temp_klv.name) > 0:
            print(f"✓ Extracted {os.path.getsize(temp_klv.name):,} bytes of KLV data\n")
            return temp_klv.name
        else:
            print("✗ No KLV data stream found in video")
            os.unlink(temp_klv.name)
            return None
            
    except subprocess.TimeoutExpired:
        print("✗ Extraction timeout")
        os.unlink(temp_klv.name)
        return None
    except Exception as e:
        print(f"✗ Error extracting KLV: {e}")
        if os.path.exists(temp_klv.name):
            os.unlink(temp_klv.name)
        return None


def read_klv_file(klv_file_path, is_temp=False):
    """Read and parse KLV packets from binary file"""
    print(f"\n{'='*70}")
    print(f"KLV Metadata Reader".center(70))
    print(f"{'='*70}\n")
    
    if not is_temp:
        print(f"File: {os.path.basename(klv_file_path)}")
    
    if not os.path.exists(klv_file_path):
        print(f"Error: File not found: {klv_file_path}")
        return None
    
    file_size = os.path.getsize(klv_file_path)
    print(f"Size: {file_size:,} bytes\n")
    
    try:
        with open(klv_file_path, 'rb') as f:
            klv_data = f.read()
    except Exception as e:
        print(f"Error reading file: {e}")
        return None

    # Framing/decoding lives in shared.klv.parse_klv_packets (single source of truth).
    packets = parse_klv_packets(klv_data)
    
    print(f"Total Packets: {len(packets)}\n")
    
    if not packets:
        print("No packets found in file")
        return
    
    # Display summary
    print(f"{'='*70}")
    print("Packet Summary")
    print(f"{'='*70}\n")
    
    # Show first packet in detail
    print("First Packet (detailed):")
    print("-" * 70)
    first = packets[0]
    print(f"Timestamp: {first.get('timestamp', 'N/A')}")
    print(f"Raw Size: {first.get('raw_size', 0)} bytes")
    print(f"Tags Found: {len(first.get('tags', {}))}\n")
    
    if 'tags' in first:
        for tag_name, tag_info in sorted(first['tags'].items()):
            value = tag_info.get('value')
            
            # Format timestamp nicely
            if tag_name == 'UNIX Time Stamp' and isinstance(value, (int, float)):
                dt = datetime.fromtimestamp(value, tz=timezone.utc)
                print(f"  {tag_name:30s}: {dt.isoformat()} ({value:.6f})")
            # Format coordinates
            elif 'Latitude' in tag_name or 'Longitude' in tag_name:
                if isinstance(value, (int, float)):
                    print(f"  {tag_name:30s}: {value:.6f}°")
                else:
                    print(f"  {tag_name:30s}: {value}")
            # Format altitude/elevation
            elif 'Altitude' in tag_name or 'Elevation' in tag_name:
                if isinstance(value, (int, float)):
                    print(f"  {tag_name:30s}: {value:.2f} m")
                else:
                    print(f"  {tag_name:30s}: {value}")
            # Format angles
            elif 'Angle' in tag_name or 'Heading' in tag_name:
                if isinstance(value, (int, float)):
                    print(f"  {tag_name:30s}: {value:.2f}°")
                else:
                    print(f"  {tag_name:30s}: {value}")
            else:
                print(f"  {tag_name:30s}: {value}")
    
    # Show timing information
    if len(packets) > 1:
        print(f"\n{'-'*70}")
        print("Last Packet (summary):")
        print("-" * 70)
        last = packets[-1]
        print(f"Timestamp: {last.get('timestamp', 'N/A')}")
        print(f"Raw Size: {last.get('raw_size', 0)} bytes")
        
        # Calculate timing
        if 'tags' in first and 'tags' in last:
            first_time = first['tags'].get('UNIX Time Stamp', {}).get('value')
            last_time = last['tags'].get('UNIX Time Stamp', {}).get('value')
            
            if first_time and last_time:
                duration = last_time - first_time
                rate = (len(packets) - 1) / duration if duration > 0 else 0
                print(f"\nTiming Information:")
                print(f"  Duration: {duration:.2f} seconds")
                print(f"  Update Rate: {rate:.2f} Hz")
                print(f"  Avg Interval: {1000/rate:.1f} ms" if rate > 0 else "  Avg Interval: N/A")
    
    print(f"\n{'='*70}\n")
    
    # Option to export as JSON
    return packets


def export_json(packets, output_file):
    """Export packets to JSON file"""
    with open(output_file, 'w') as f:
        json.dump(packets, f, indent=2, default=str)
    print(f"Exported {len(packets)} packets to: {output_file}")


def main():
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Read and display KLV metadata from video files or binary KLV files'
    )
    parser.add_argument(
        'input_file',
        help='Video file (.mp4, .ts, .mov, etc.) or KLV binary file (.bin)'
    )
    parser.add_argument(
        '--json',
        help='Export to JSON file'
    )
    parser.add_argument(
        '--packet',
        type=int,
        help='Show detailed info for specific packet number'
    )
    
    args = parser.parse_args()
    
    if not os.path.exists(args.input_file):
        print(f"Error: File not found: {args.input_file}")
        return 1
    
    # Check if input is a video file
    temp_klv_file = None
    if is_video_file(args.input_file):
        # Extract KLV from video
        temp_klv_file = extract_klv_from_video(args.input_file)
        if not temp_klv_file:
            print("\nNo KLV metadata found in video file")
            return 1
        klv_file = temp_klv_file
        is_temp = True
    else:
        # Direct KLV binary file
        klv_file = args.input_file
        is_temp = False
    
    try:
        # Read and parse the file
        packets = read_klv_file(klv_file, is_temp=is_temp)
        
        if not packets:
            print("No packets found or failed to parse")
            return 1
        
        if args.json:
            export_json(packets, args.json)
        
        if args.packet is not None:
            if 0 <= args.packet < len(packets):
                print(f"\n{'='*70}")
                print(f"Packet {args.packet} (detailed):")
                print(f"{'='*70}\n")
                print(json.dumps(packets[args.packet], indent=2, default=str))
            else:
                print(f"\nError: Packet {args.packet} out of range (0-{len(packets)-1})")
        
        return 0
        
    finally:
        # Clean up temporary file if created
        if temp_klv_file and os.path.exists(temp_klv_file):
            os.unlink(temp_klv_file)


if __name__ == "__main__":
    main()
