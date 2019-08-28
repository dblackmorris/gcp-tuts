#GSutil commands
gsutil ls #list bucket
gsutil mb -l us-east1 gs://gcp-ls-d4nt3 #where mb = make bucket, -l = location,gs://gcp-ls-d4nt3 = url for new bucket
gsutil cp README-cloudshell.txt gs://gcp-ls-d4nt3/ #where cp=copy 
gsutil ls gs://gcp-ls-d4nt3/ # where ls is list and used here for list all object inside the bucket
gsutil cp README-cloudshell.txt gs://gcp-ls-d4nt3/test/ # this command will be used for copy file inside test directory if present or create dir named test and then copy file inside that
gsutil label get gs://gcp-ls-d4nt3/ # for getting tag configuration of that bucket
gsutil label set bucketlabels.json gs://gcp-ls-d4nt3/ # for setting tag on that bucket where bucketlabel.json is file with all tagging information
