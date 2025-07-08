import os
import json
import logging
import boto3
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get('LOG_LEVEL', 'INFO').upper())

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
        # Re-raise the exception to be caught by the main handler's try-except block
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
            logger.info(f"Found listener: '{listener_arn}'")

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

def handler(event, context):
    """
    Lambda function entry point to retrieve ALB ARN and associated Target Group ARNs.
    """
    load_balancer_name = os.environ.get('LOAD_BALANCER_NAME')

    if not load_balancer_name:
        logger.error("LOAD_BALANCER_NAME environment variable is not set.")
        return {
            'statusCode': 400,
            'body': json.dumps('Error: LOAD_BALANCER_NAME environment variable is missing.')
        }

    try:
        alb_arn = get_load_balancer_arn(load_balancer_name)

        if not alb_arn:
            return {
                'statusCode': 404,
                'body': json.dumps(f"ALB '{load_balancer_name}' not found.")
            }

        target_group_arns = get_target_group_arns_from_alb(alb_arn)

        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'ALB and Target Group ARNs retrieved successfully',
                'alb_arn': alb_arn,
                'target_group_arns': target_group_arns
            })
        }
    except Exception as e:
        logger.error(f"Lambda execution failed: {e}", exc_info=True)
        return {
            'statusCode': 500,
            'body': json.dumps(f'Internal Server Error: {e}')
        }
