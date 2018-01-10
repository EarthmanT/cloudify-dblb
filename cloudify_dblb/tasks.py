#!/usr/bin/env python

import time
from cloudify.workflows import ctx
from cloudify.decorators import workflow
from cloudify.manager import get_rest_client
from requests.exceptions import ConnectionError
from cloudify.exceptions import OperationRetry
from cloudify_rest_client.exceptions import CloudifyClientError


def check_api(client_callable, arguments=None, _progress_handler=None):
    """ Check for API Response and handle generically. """

    try:
        if isinstance(arguments, dict):
            response = client_callable(**arguments)
        elif arguments is None:
            response = client_callable()
        elif _progress_handler is not None:
            response = client_callable(
                arguments, progress_callback=_progress_handler)
        else:
            response = client_callable(arguments)
    except ConnectionError as e:
        raise OperationRetry('Retrying after error: {0}'.format(str(e)))
    except CloudifyClientError as e:
        if e.status_code == 502:
            raise OperationRetry('Retrying after error: {0}'.format(str(e)))
        else:
            ctx.logger.error('Ignoring error: {0}'.format(str(e)))
    else:
        ctx.logger.debug('Returning response: {0}'.format(response))
        return response
    return None


def wait_for_execution_termination(
        client, execution_id, timeout=300, interval=15):
    start_time = time.time()
    execution_arguments = {
        'execution_id': execution_id
    }
    while True:
        elapsed_time = time.time() - start_time
        execution_response = \
            check_api(client.executions.get, execution_arguments)
        if execution_response is None:
            break
        elif 'failed' in execution_response.get('status'):
            break
        elif 'terminated' in execution_response.get('status'):
            break
        elif elapsed_time > timeout:
            break
        else:
            time.sleep(interval)


@workflow
def scale_and_update(
        db_deployment_id, lb_deployment_id, scale_parameters, **_):
    """
    db_deployment_id: The MariaDB Deployment ID.
    lb_deployment_id: The HAProxy Deployment ID.
    scale_parameters: The parameters to send to the scale workflow.
    """

    client = get_rest_client()

    # Scale the Database Deployment.
    db_scale_arguments = {
        'deployment_id': db_deployment_id,
        'workflow_id': 'scale',
        'parameters': scale_parameters
    }
    db_scale_response = check_api(client.executions.start, db_scale_arguments)
    db_scale_id = db_scale_response.get('id')
    wait_for_execution_termination(client, db_scale_id)

    # Get the Database Deployment Outputs.
    dp_get_arguments = {
        'deployment_id': db_deployment_id
    }
    db_get_response = \
        check_api(client.deployments.outputs.get, dp_get_arguments)
    cluster_addresses = db_get_response['outputs']['cluster_addresses']

    # Get the current backend list of the load balancer.
    node_instances = check_api(client.node_instances.list)
    for node_instance in node_instances:
        if lb_deployment_id not in node_instance['deployment_id']:
            continue
        if 'backends' in node_instance['runtime_properties']:
            break
    backends = node_instance['runtime_properties']['backends']
    if not isinstance(backends, dict):
        return
    backend_addresses = \
        [backends[backend_id]['address'] for backend_id in backends.keys()]

    # For each new address that is not currently in the backend, update the lb.
    for cluster_address in cluster_addresses:
        if cluster_address in backend_addresses:
            continue
        else:
            execute_operation_arguments = {
                'deployment_id': lb_deployment_id,
                'workflow_id': 'execute_operation',
                'parameters': {
                    'node_instance_ids': node_instance['id'],
                    'operation': 'create',
                    'allow_kwargs_override': True,
                    'operation_kwargs': {
                        'frontend_port': 3306,
                        'update_backends': {
                            cluster_address: {
                                'address': cluster_address,
                                'port': 3306,
                                'maxconn': 32
                            }
                        }
                    }
                }
            }
        lb_update_response = \
            check_api(client.executions.start, execute_operation_arguments)
        lb_update_id = lb_update_response.get('id')
        wait_for_execution_termination(client, lb_update_id)
