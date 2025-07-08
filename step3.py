import os
import json
import logging
import boto3
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO').upper()) # Set to 'DEBUG' for more verbosity during testing

# Initialize the ELBv2 client (boto3 is synchronous by default)
elbv2_client = boto3.client('elbv2')

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
            logger.debug(f"Found listener: '{listener_arn}'") # Changed to debug

            # Describe rules for each listener to get target groups
            # This includes the default rule which always has a target group action
            rules_response = elbv2_client.describe_rules(ListenerArn=listener_arn)

            for rule in rules_response.get('Rules', []):
                for action in rule.get('Actions', []):
                    if action['Type'] == 'forward' and 'TargetGroupArn' in action:
                        target_group_arns.add(action['TargetGroupArn'])
                        logger.debug(f"Found target group ARN: '{action['TargetGroupArn']}' from rule: '{rule['RuleArn']}'")
                    # Handle weighted target groups (if applicable)
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
        logger.info(f"Describing target health for: '{target_group_arn}'")
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

def handler(event, context):
    """
    Lambda function entry point to retrieve ALB ARN, Target Group ARNs,
    and check health of one sample target group.
    """
    load_balancer_name = os.environ.get('LOAD_BALANCER_NAME')

    if not load_balancer_name:
        logger.error("LOAD_BALANCER_NAME environment variable is not set.")
        return {
            'statusCode': 400,
            'body': json.dumps('Error: LOAD_BALANCER_NAME environment variable is missing.')
        }

    try:
        # Step 1: Get ALB ARN
        alb_arn = get_load_balancer_arn(load_balancer_name)
        if not alb_arn:
            return {
                'statusCode': 404,
                'body': json.dumps(f"ALB '{load_balancer_name}' not found.")
            }

        # Step 2: Get Target Group ARNs
        target_group_arns = get_target_group_arns_from_alb(alb_arn)
        if not target_group_arns:
            logger.warning(f"No target groups found for ALB '{load_balancer_name}'.")
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'message': f"No target groups found for ALB '{load_balancer_name}'.",
                    'alb_arn': alb_arn,
                    'target_group_arns': []
                })
            }

        # Step 3: Check health of a single (first) target group for demonstration
        # In Step 4, we will iterate through ALL of them.
        sample_tg_arn = target_group_arns[0]
        sample_tg_is_healthy = is_target_group_healthy(sample_tg_arn)

        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'ALB and Target Group info retrieved. Sample TG health checked.',
                'alb_arn': alb_arn,
                'total_target_groups_found': len(target_group_arns),
                'sample_target_group_arn_checked': sample_tg_arn,
                'sample_target_group_is_healthy': sample_tg_is_healthy
            })
        }
    except Exception as e:
        logger.error(f"Lambda execution failed: {e}", exc_info=True)
        return {
            'statusCode': 500,
            'body': json.dumps(f'Internal Server Error: {e}')
        }
