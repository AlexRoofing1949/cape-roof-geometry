"""Synthetic native-runtime acceptance test; NOT a live-property accuracy claim.
Runs the production PDAL filters against a local LAS instead of downloading EPT,
then runs real Roofer and Open3D. No mocked geometry, density or QA attributes.
"""
import csv
import json
import math
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from shapely.geometry import box
from app import pipeline
from app.cityjson_geometry import load_cityjson_feature, extract_roof_geometry
from app.plane_validation import validate_roofer_planes
from app.providers import FootprintResult, transform_geometry, write_footprint_inputs


def main():
    s=SimpleNamespace(command_timeout_seconds=180, minimum_roof_hag_meters=1.5,
        maximum_roof_hag_meters=40, roof_cluster_tolerance_meters=1.5,
        minimum_roof_cluster_points=20, lidar_buffer_meters=8,
        roofer_plane_detect_epsilon_meters=0.15, roofer_complexity_factor=0.95,
        open3d_version='0.19.0', open3d_minimum_facet_points=20,
        open3d_minimum_inlier_ratio=0.65, open3d_maximum_assignment_distance_meters=0.6,
        open3d_distance_threshold_meters=0.05, open3d_maximum_normal_variance_degrees=5,
        open3d_maximum_plane_rmse_meters=0.05, open3d_ransac_iterations=500)
    with tempfile.TemporaryDirectory() as directory:
        w=Path(directory); source=w/'input.csv'; las=w/'input.laz'
        with source.open('w') as output:
            writer=csv.writer(output);writer.writerow(['X','Y','Z','Classification'])
            for ix in range(-32,113):
                for iy in range(-32,73):
                    x=ix/4; y=iy/4
                    if not (0 <= x <= 20 and 0 <= y <= 10):
                        writer.writerow([500000+x,2940000+y,0,2])
            for ix in range(81):
                for iy in range(41):
                    x=ix/4;y=iy/4
                    writer.writerow([500000+x,2940000+y,10+min(y,10-y)*0.5,1])
        writer_pipeline=w/'input-pipeline.json'
        writer_pipeline.write_text(json.dumps({'pipeline':[
            {'type':'readers.text','filename':str(source),'override_srs':'EPSG:32617'},
            {'type':'writers.las','filename':str(las),'a_srs':'EPSG:32617',
             'minor_version':4,'dataformat_id':6,'scale_x':0.001,'scale_y':0.001,'scale_z':0.001}]}))
        subprocess.run(['pdal','pipeline',str(writer_pipeline)],check=True)
        outline=transform_geometry(box(500000,2940000,500020,2940010),'EPSG:32617','EPSG:4326')
        fp=FootprintResult(outline,'synthetic-gable','synthetic',0,[])
        lon,lat=outline.centroid.x,outline.centroid.y
        gpkg,crop_wkt,epsg,projected=write_footprint_inputs(fp,'native-smoke',w,lon,lat,s)
        lidar=SimpleNamespace(ept_url='https://example.invalid/ept.json', allowed_classes=(1,2,6),
            roof_classes=(1,6),source_id='synthetic',acquired_start='2022-01-01',acquired_end='2022-12-31')
        original_run=pipeline._run
        def local_reader(command, **kwargs):
            path=Path(command[-1]); content=json.loads(path.read_text())
            if content['pipeline'][0]['type']=='readers.ept':
                content['pipeline'][0]={'type':'readers.las','filename':str(las)}
                path.write_text(json.dumps(content))
            return original_run(command,**kwargs)
        cropped=w/'cropped.laz'
        with patch('app.pipeline._run', side_effect=local_reader):
            audit=pipeline._pdal_crop(lidar,crop_wkt,epsg,cropped,w,s)
        assert audit['classHistogram'].get('2',0)>0, 'Ground points were lost'
        density=pipeline._roofprint_point_density(cropped,projected,w,s)
        assert density['pointDensityPpsm']>8, density
        feature_path,metadata_path=pipeline._run_roofer(cropped,gpkg,w/'reconstructed',s)
        feature,transform=load_cityjson_feature(feature_path,metadata_path)
        geometry=extract_roof_geometry(feature,transform,include_validation_facets=True)
        independent=validate_roofer_planes(cropped,geometry.pop('_validationFacets'),w,s)
        expected_area=200*math.sqrt(1.25)*10.763910416709722
        assert abs(geometry['roofAreaSqFt']/expected_area-1)<0.05, geometry['roofAreaSqFt']
        assert abs(geometry['averagePitchDegrees']-math.degrees(math.atan(0.5)))<2, geometry['averagePitchDegrees']
        for key,meters in [('ridgesFeet',20),('eavesFeet',40),('rakesFeet',4*math.sqrt(5**2+2.5**2))]:
            assert abs(geometry[key]/(meters*3.280839895)-1)<0.05,(key,geometry[key])
        print(json.dumps({'fixture':'SYNTHETIC_NOT_LIVE_PROPERTY','result':'PASS',
            'groundCount':audit['classHistogram']['2'],'density':density,
            'geometry':geometry,'independentPlaneValidation':independent},indent=2))

if __name__=='__main__':
    main()
