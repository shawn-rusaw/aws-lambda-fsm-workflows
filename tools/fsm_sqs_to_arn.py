# system imports
import argparse
import json
import logging
import sys
import time

# library imports

# application imports
from aws_lambda_fsm.constants import AWS_SQS
from aws_lambda_fsm.constants import AWS_KINESIS
from aws_lambda_fsm.constants import AWS
from aws_lambda_fsm.constants import PAYLOAD
from aws_lambda_fsm.constants import SYSTEM_CONTEXT
from aws_lambda_fsm.aws import get_connection
from aws_lambda_fsm.aws import get_arn_from_arn_string

import settings

ALLOWED_DEST_SERVICES = [AWS.KINESIS, AWS.SNS]

# TODO: local cache of processed message ids
# TODO: handle partial batch kinesis failure
# TODO: handle partial sns failure
# TODO: handle partial batch sqs failure
# TODO: start via supervisord

# setup the command line args
parser = argparse.ArgumentParser(description='Forwards messages from SQS to dest arn.')
parser.add_argument('--sqs_queue_arn', default='PRIMARY_STREAM_SOURCE')
parser.add_argument('--dest_arn', default='SECONDARY_STREAM_SOURCE')
parser.add_argument('--log_level', default='INFO')
parser.add_argument('--boto_log_level', default='INFO')
args = parser.parse_args()

logging.basicConfig(
    format='[%(levelname)s] %(asctime)-15s %(message)s',
    level=int(args.log_level) if args.log_level.isdigit() else args.log_level,
    datefmt='%Y-%m-%d %H:%M:%S'
)

logging.getLogger('boto3').setLevel(args.boto_log_level)
logging.getLogger('botocore').setLevel(args.boto_log_level)

# setup connections to AWS
sqs_arn_string = getattr(settings, args.sqs_queue_arn)
sqs_arn = get_arn_from_arn_string(sqs_arn_string)
if sqs_arn.service != AWS.SQS:
    logging.fatal("%s is not an SQS ARN", sqs_arn_string)
    sys.exit(1)
sqs_conn = get_connection(sqs_arn_string)
response = sqs_conn.get_queue_url(
    QueueName=sqs_arn.resource.split(':')[-1]
)
sqs_queue_url = response[AWS_SQS.QueueUrl]

logging.info('SQS ARN: %s', sqs_arn_string)
logging.info('SQS endpoint: %s', settings.ENDPOINTS.get(AWS.SQS))
logging.info('SQS queue: %s', sqs_arn.resource)
logging.info('SQS queue url: %s', sqs_queue_url)

dest_arn_string = getattr(settings, args.dest_arn)
dest_arn = get_arn_from_arn_string(dest_arn_string)
if dest_arn.service not in ALLOWED_DEST_SERVICES:
    logging.fatal(
        "%s is not a %s ARN",
        dest_arn_string,
        '/'.join(map(str.upper, ALLOWED_DEST_SERVICES))
    )
    sys.exit(1)
dest_conn = get_connection(dest_arn_string)

logging.info('Dest ARN: %s', dest_arn_string)
logging.info('Dest endpoint: %s', settings.ENDPOINTS.get(dest_arn.service))
logging.info('Dest resource: %s', dest_arn.resource)

wait = 5
backoff = 0

# this service will run forever, echoing messages from SQS
# onto another Amazon service.
while True:

    try:
        # set the backoff in case of error
        backoff += 5

        # receive up to 10 messages from SQS
        sqs_messages = []
        response = sqs_conn.receive_message(
            QueueUrl=sqs_queue_url,
            MaxNumberOfMessages=10
        )
        sqs_messages = response.get(AWS_SQS.Messages, [])

        # if not SQS messages were received, then we simply wait a little bit
        # and try again. we wait to avoid hitting the SQS endpoint too often.
        if not sqs_messages:
            logging.info('No messages. Sleeping for %d seconds', wait)
            time.sleep(wait)
            continue

        # now echo them one at a time to SNS
        logging.info('Echoing %d messages from %s to %s...',
                     len(sqs_messages),
                     sqs_arn_string,
                     dest_arn_string)

        # echo to SNS
        if dest_arn.service == AWS.SNS:
            for sqs_message in sqs_messages:
                response = dest_conn.publish(
                    TopicArn=dest_arn,
                    Message=sqs_message[AWS_SQS.MESSAGE.Body],
                )
                logging.info(response)

        # echo to Kinesis
        elif dest_arn.service == AWS.KINESIS:
            records = []
            for sqs_message in sqs_messages:
                body = sqs_message[AWS_SQS.MESSAGE.Body]
                payload = json.loads(body)
                system_context = payload[PAYLOAD.SYSTEM_CONTEXT]
                correlation_id = system_context[SYSTEM_CONTEXT.CORRELATION_ID]
                response = records.append(
                    {
                        AWS_KINESIS.RECORD.Data: body,
                        AWS_KINESIS.RECORD.PartitionKey: correlation_id
                    }
                )
            response = dest_conn.put_records(
                StreamName=dest_arn.resource.split('/')[-1],
                Records=records
            )
            logging.info(response)

        # after processing, the SQS messages need to be deleted
        sqs_conn.delete_message_batch(
            QueueUrl=sqs_queue_url,
            Entries=[
                {
                    AWS_SQS.MESSAGE.Id: str(i),
                    AWS_SQS.MESSAGE.ReceiptHandle: sqs_message[AWS_SQS.MESSAGE.ReceiptHandle]
                }
                for i, sqs_message in enumerate(sqs_messages)
            ]
        )
        backoff = 0

    except Exception:

        logging.exception('Exception occurred. Sleeping for %d seconds', backoff)
        time.sleep(backoff)
