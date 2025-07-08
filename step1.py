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
        logger.error(f"An unexpected error occurred: {e}", exc_info=True)
        raise

def handler(event, context):
    """
    Lambda function entry point to retrieve ALB ARN.
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

        if alb_arn:
            return {
                'statusCode': 200,
                'body': json.dumps({'message': 'ALB ARN retrieved successfully', 'alb_arn': alb_arn})
            }
        else:
            return {
                'statusCode': 404,
                'body': json.dumps(f"ALB '{load_balancer_name}' not found.")
            }
    except Exception as e:
        logger.error(f"Lambda execution failed: {e}")
        return {
            'statusCode': 500,
            'body': json.dumps(f'Internal Server Error: {e}')
        }
