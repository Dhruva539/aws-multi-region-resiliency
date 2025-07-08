import os
import json
import logging
import boto3
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO').upper()) # Set to 'DEBUG' for more verbosity during testing

# Initialize AWS clients
elbv2_client = boto3.client('elbv2')
cloudwatch_client = boto3.client('cloudwatch')

# --- Helper Functions ---

def get_load_balancer_arn(load_balancer_name):
    """
    Retrieves the ARN of an ALB given its name.
    Returns None if not found or an error occurs.
    """
    try:
        logger.info(f"Attempting to describe load balancer: '{load_balancer_name}'")
        response = elbv2_client.describe_load_balancers(Names=[load_balancer_name])

        if response['LoadBalancers']:
            alb_arn = response['LoadBalancers'][0]['LoadBalancerArn']
            logger.info(f"Found ALB '{load_balancer_name}' with ARN: '{alb_arn}'")
            return alb_arn
        else:
            logger.warning(f"Load balancer '{load_balancer_name}' not found.")
            return None
    except ClientError as e:
        logger.error(f"AWS API Error describing load balancer '{load_balancer_name}': {e}")
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred in get_load_balancer_arn: {e}", exc_info=True)
        raise

def get_target_group_arns_from_alb(alb_arn):
    """
    Retrieves a unique list of Target Group ARNs associated with an ALB.
    This involves describing listeners and then their rules.
    """
    target_group_arns = set() # Use a set to store unique ARNs

    try:
        logger.info(f"Describing listeners for ALB: '{alb_arn}'")
        listeners_response = elbv2_client.describe_listeners(LoadBalancerArn=alb_arn)

        for listener in listeners_response.get('Listeners', []):
            listener_arn = listener['ListenerArn']
            logger.debug(f"Found listener: '{listener_arn}'")

            # Describe rules for each listener to get target groups
            rules_response = elbv2_client.describe_rules(ListenerArn=listener_arn)

            for rule in rules_response.get('Rules', []):
                for action in rule.get('Actions', []):
                    if action['Type'] == 'forward' and 'TargetGroupArn' in action:
                        target_group_arns.add(action['TargetGroupArn'])
                        logger.debug(f"Found target group ARN: '{action['TargetGroupArn']}' from rule: '{rule['RuleArn']}'")
                    elif action['Type'] == 'forward' and 'TargetGroupStickinessConfig' in action and 'TargetGroups' in action['ForwardConfig']:
                         for tg_in_forward in action['ForwardConfig']['TargetGroups']:
                             if 'TargetGroupArn' in tg_in_forward:
                                 target_group_arns.add(tg_in_forward['TargetGroupArn'])
                                 logger.debug(f"Found target group ARN (weighted): '{tg_in_forward['TargetGroupArn']}' from rule: '{rule['RuleArn']}'")

        logger.info(f"Finished collecting target group ARNs. Total unique ARNs found: {len(target_group_arns)}")
        return list(target_group_arns) # Convert set to list for consistent return type
    except ClientError as e:
        logger.error(f"AWS API Error describing listeners or rules for ALB '{alb_arn}': {e}")
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred in get_target_group_arns_from_alb: {e}", exc_info=True)
        raise

def is_target_group_healthy(target_group_arn):
    """
    Checks if a target group has at least one healthy target.
    Returns True if healthy, False otherwise.
    """
    try:
        logger.debug(f"Describing target health for: '{target_group_arn}'")
        health_response = elbv2_client.describe_target_health(TargetGroupArn=target_group_arn)

        healthy_targets_count = 0
        total_targets_count = 0

        for target_health in health_response.get('TargetHealthDescriptions', []):
            total_targets_count += 1
            state = target_health['TargetHealth']['State']
            target_id = target_health['Target']['Id']
            logger.debug(f"Target '{target_id}' state: '{state}' (Target Group: '{target_group_arn}')")

            if state == 'healthy':
                healthy_targets_count += 1

        if total_targets_count == 0:
            logger.warning(f"Target Group '{target_group_arn}' has no registered targets.")
            return False # No targets means not healthy in this context
        elif healthy_targets_count > 0:
            logger.info(f"Target Group '{target_group_arn}' is HEALTHY ({healthy_targets_count}/{total_targets_count} healthy targets).")
            return True
        else:
            logger.warning(f"Target Group '{target_group_arn}' is UNHEALTHY (0/{total_targets_count} healthy targets).")
            return False

    except ClientError as e:
        logger.error(f"AWS API Error describing target health for '{target_group_arn}': {e}")
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred in is_target_group_healthy: {e}", exc_info=True)
        raise

def publish_cloudwatch_metric(namespace, metric_name, value, unit, dimensions):
    """
    Publishes a custom metric to CloudWatch.
    """
    try:
        cloudwatch_client.put_metric_data(
            Namespace=namespace,
            MetricData=[
                {
                    'MetricName': metric_name,
                    'Dimensions': dimensions,
                    'Value': float(value),
                    'Unit': unit
                },
            ]
        )
        logger.info(f"Published metric '{metric_name}' (Value: {value}, Unit: {unit}) to namespace '{namespace}' with dimensions {dimensions}")
    except ClientError as e:
        logger.error(f"AWS API Error publishing CloudWatch metric '{metric_name}': {e}")
    except Exception as e:
        logger.error(f"An unexpected error occurred while publishing CloudWatch metric: {e}", exc_info=True)


# --- Main Lambda Handler ---

def handler(event, context):
    """
    Lambda function entry point to perform comprehensive ALB health check
    and publish a binary health metric to CloudWatch.
    """
    load_balancer_name = os.environ.get('LOAD_BALANCER_NAME')
    healthy_threshold_percentage_str = os.environ.get('HEALTHY_THRESHOLD_PERCENTAGE', '75')
    
    # Directly use the provided namespace
    cloudwatch_namespace = "CTSI/HealthChecks" # Confirmed namespace

    if not load_balancer_name:
        logger.error("LOAD_BALANCER_NAME environment variable is not set.")
        return {
            'statusCode': 400,
            'body': json.dumps('Error: LOAD_BALANCER_NAME environment variable is missing.')
        }

    try:
        healthy_threshold_percentage = float(healthy_threshold_percentage_str)
        if not (0 <= healthy_threshold_percentage <= 100):
            raise ValueError("HEALTHY_THRESHOLD_PERCENTAGE must be between 0 and 100.")
    except ValueError as e:
        logger.error(f"Invalid HEALTHY_THRESHOLD_PERCENTAGE: {e}")
        return {
            'statusCode': 400,
            'body': json.dumps(f'Error: Invalid HEALTHY_THRESHOLD_PERCENTAGE environment variable: {e}')
        }

    # Initialize metric value to 0 (unhealthy) by default, then set to 1 if healthy
    binary_health_metric_value = 0
    overall_status = "UNHEALTHY"
    status_code = 500

    alb_arn = None # Initialize alb_arn outside try block for later use

    try:
        # Step 1: Get ALB ARN
        alb_arn = get_load_balancer_arn(load_balancer_name)
        if not alb_arn:
            logger.error(f"ALB '{load_balancer_name}' not found. Cannot proceed with health check.")
            # Publish 0 for BinaryHealthCheck if ALB not found (no dimensions)
            publish_cloudwatch_metric(
                cloudwatch_namespace,
                'BinaryHealthCheck',
                0, # 0 means unhealthy
                'Count',
                [] # <--- NO DIMENSIONS HERE
            )
            return {
                'statusCode': 404,
                'body': json.dumps(f"ALB '{load_balancer_name}' not found. Published 0 to BinaryHealthCheck metric in '{cloudwatch_namespace}'.")
            }

        # Step 2: Get Target Group ARNs
        target_group_arns = get_target_group_arns_from_alb(alb_arn)

        healthy_tg_count = 0
        total_tg_count = len(target_group_arns)

        if total_tg_count == 0:
            logger.warning(f"No target groups found for ALB '{load_balancer_name}'. Considering 100% healthy (no TGs).")
            healthy_percentage = 100.0
        else:
            # Step 3: Check health of each target group
            for tg_arn in target_group_arns:
                if is_target_group_healthy(tg_arn):
                    healthy_tg_count += 1

            healthy_percentage = (healthy_tg_count / total_tg_count) * 100

        logger.info(f"Overall ALB health: {healthy_tg_count}/{total_tg_count} target groups healthy ({healthy_percentage:.2f}%).")

        # Determine overall status and binary metric value
        if healthy_percentage >= healthy_threshold_percentage:
            overall_status = "HEALTHY"
            status_code = 200
            binary_health_metric_value = 1 # 1 means healthy
        else:
            overall_status = "UNHEALTHY"
            status_code = 500
            binary_health_metric_value = 0 # 0 means unhealthy
            logger.error(f"ALB health ({healthy_percentage:.2f}%) is below threshold ({healthy_threshold_percentage}%).")

        # Publish the BinaryHealthCheck metric (no dimensions)
        publish_cloudwatch_metric(
            cloudwatch_namespace,
            'BinaryHealthCheck',
            binary_health_metric_value,
            'Count', # Unit remains 'Count'
            [] # <--- NO DIMENSIONS HERE
        )

        return {
            'statusCode': status_code,
            'body': json.dumps({
                'message': f"ALB health check completed. Overall status: {overall_status}.",
                'alb_name': load_balancer_name,
                'alb_arn': alb_arn,
                'total_target_groups': total_tg_count,
                'healthy_target_groups': healthy_tg_count,
                'healthy_percentage': f"{healthy_percentage:.2f}%",
                'threshold_percentage': f"{healthy_threshold_percentage}%",
                'overall_status': overall_status,
                'published_binary_health_value': binary_health_metric_value,
                'published_cloudwatch_namespace': cloudwatch_namespace
            })
        }

    except Exception as e:
        logger.error(f"Lambda execution failed during overall health check: {e}", exc_info=True)
        
        # Publish 0 to BinaryHealthCheck metric on general failure (no dimensions)
        publish_cloudwatch_metric(
            cloudwatch_namespace,
            'BinaryHealthCheck',
            0, # 0 means unhealthy on error
            'Count',
            [] # <--- NO DIMENSIONS HERE
        )
        return {
            'statusCode': 500,
            'body': json.dumps(f'Internal Server Error: {e}')
        }
