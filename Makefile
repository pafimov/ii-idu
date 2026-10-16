.PHONY: run test check package
run:
	python3 mvp/app.py

test:
	python3 -m unittest discover -s mvp -v

check:
	node --check mvp/static/app.js
	node --check mvp/static/broker.js
	node --check mvp/static/sandbox.js

package:
	python3 mvp/scripts/package.py
