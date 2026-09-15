from copy import deepcopy
import pytest
import yaml
from jenkins.python.llm_agent_cd.serving_recovery_deploy import image_only, NAME


def test_serving_repair_is_image_only():
    a={'kind':'Deployment','metadata':{'name':NAME},'spec':{'template':{'spec':{'containers':[
        {'name':'api','image':'old','env':[{'name':'IMAGE_REFERENCE','value':'old'}],
         'resources':{'requests':{'cpu':'100m'}}}]}}}}
    b=deepcopy(a);api=b['spec']['template']['spec']['containers'][0]
    api['image']='new';api['env'][0]['value']='new'
    image_only(yaml.safe_dump(a),yaml.safe_dump(b),'new')
    api['resources']['requests']['cpu']='50m'
    with pytest.raises(ValueError,match='more than'):image_only(yaml.safe_dump(a),yaml.safe_dump(b),'new')
